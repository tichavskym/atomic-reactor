"""
Copyright (c) 2017-2022 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.

Takes the filesystem image created by the Dockerfile generated by
pre_flatpak_create/update_dockerfile, extracts the tree at /var/tmp/flatpak-build
and turns it into a Flatpak application or runtime.
"""
import functools
import os.path
import shutil
import subprocess
import tempfile
from typing import Any, Dict, Optional

from atomic_reactor.utils import retries
from flatpak_module_tools.flatpak_builder import FlatpakBuilder, FLATPAK_METADATA_ANNOTATIONS

from atomic_reactor.constants import (IMAGE_TYPE_OCI,
                                      PLUGIN_FLATPAK_CREATE_OCI,
                                      PLUGIN_RESOLVE_COMPOSES_KEY)
from atomic_reactor.dirs import BuildDir
from atomic_reactor.plugin import Plugin
from atomic_reactor.util import get_exported_image_metadata, is_flatpak_build
from atomic_reactor.utils.flatpak_util import FlatpakUtil
from atomic_reactor.utils.rpm import parse_rpm_output


class FlatpakCreateOciPlugin(Plugin):
    key = PLUGIN_FLATPAK_CREATE_OCI
    is_allowed_to_fail = False

    def __init__(self, workflow):
        """
        :param workflow: DockerBuildWorkflow instance
        """
        super(FlatpakCreateOciPlugin, self).__init__(workflow)

        try:
            self.flatpak_metadata = self.workflow.conf.flatpak_metadata
        except KeyError:
            self.flatpak_metadata = FLATPAK_METADATA_ANNOTATIONS

    def build_flatpak_image(self, source, build_dir: BuildDir) -> Dict[str, Any]:
        builder = FlatpakBuilder(source, build_dir.path,
                                 'var/tmp/flatpak-build',
                                 parse_manifest=parse_rpm_output,
                                 flatpak_metadata=self.flatpak_metadata)

        df_labels = build_dir.dockerfile_with_parent_env(
            self.workflow.imageutil.base_image_inspect()
        ).labels

        builder.add_labels(df_labels)

        tmp_dir = tempfile.mkdtemp(dir=build_dir.path)

        image_filesystem = self.workflow.imageutil.extract_filesystem_layer(
            str(build_dir.exported_squashed_image), str(tmp_dir))

        build_dir.exported_squashed_image.unlink()

        filesystem_path = os.path.join(tmp_dir, image_filesystem)

        with open(filesystem_path, 'rb') as f:
            # this part is 'not ideal' but this function seems to be a prerequisite
            # for building flatpak image since it does the setup for it
            flatpak_filesystem, flatpak_manifest = builder._export_from_stream(f)

        os.remove(filesystem_path)

        self.log.info('filesystem tarfile written to %s', flatpak_filesystem)

        image_rpm_components = builder.get_components(flatpak_manifest)

        ref_name, outfile, outfile_tarred = builder.build_container(flatpak_filesystem)

        os.remove(outfile_tarred)

        metadata = get_exported_image_metadata(outfile, IMAGE_TYPE_OCI)
        metadata['ref_name'] = ref_name

        cmd = ['skopeo', 'copy', 'oci:{path}:{ref_name}'.format(**metadata), '--format=v2s2',
               'docker-archive:{}'.format(str(build_dir.exported_squashed_image))]
        cleanup_cmd = ['rm', str(build_dir.exported_squashed_image)]

        try:
            retries.run_cmd(cmd, cleanup_cmd)
        except subprocess.CalledProcessError as e:
            self.log.error("skopeo copy failed with output:\n%s", e.output)
            raise RuntimeError("skopeo copy failed with output:\n{}".format(e.output)) from e

        self.log.info('OCI image is available as %s', outfile)

        shutil.rmtree(tmp_dir)

        self.workflow.data.image_components[build_dir.platform] = image_rpm_components

        return metadata

    def run(self) -> Optional[Dict[str, Dict[str, Any]]]:
        if not is_flatpak_build(self.workflow):
            self.log.info('not flatpak build, skipping plugin')
            return None

        resolve_comp_result: Dict[str, Any] = self.workflow.data.plugins_results[
            PLUGIN_RESOLVE_COMPOSES_KEY]
        flatpak_util = FlatpakUtil(workflow_config=self.workflow.conf,
                                   source_config=self.workflow.source.config,
                                   composes=resolve_comp_result['composes'])
        source = flatpak_util.get_flatpak_source_info()
        if not source:
            raise RuntimeError("flatpak_create_dockerfile must be run before flatpak_create_oci")

        build_flatpak_image = functools.partial(self.build_flatpak_image, source)

        return self.workflow.build_dir.for_each_platform(build_flatpak_image)
