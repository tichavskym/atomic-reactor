"""
Copyright (c) 2015-2022 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""
import os
import subprocess
import tempfile
from typing import List

from atomic_reactor.dirs import BuildDir
from atomic_reactor.plugin import Plugin
from atomic_reactor.types import RpmComponent
from atomic_reactor.utils.imageutil import NothingExtractedError
from atomic_reactor.utils.rpm import parse_rpm_output
from atomic_reactor.utils.rpm import rpm_qf_args

RPMDB_PATH = '/var/lib/rpm'
RPMDB_DIR_NAME = 'rpm'

__all__ = ('RPMqaPlugin',)


class RPMqaPlugin(Plugin):
    key = "all_rpm_packages"
    is_allowed_to_fail = False
    sep = ';'

    def __init__(self, workflow, ignore_autogenerated_gpg_keys=True):
        """
        constructor

        :param workflow: DockerBuildWorkflow instance
        """
        # call parent constructor
        super().__init__(workflow)
        self.ignore_autogenerated_gpg_keys = ignore_autogenerated_gpg_keys

    def run(self) -> None:
        # If another plugin has already filled in the image component list, skip
        if self.workflow.data.image_components:
            self.log.info('Another plugin has already filled in the image component list, skip')
            return None
        self.workflow.data.image_components = self.workflow.build_dir.for_each_platform(
            self.gather_output)

    def gather_output(self, build_dir: BuildDir) -> List[RpmComponent]:
        image = self.workflow.data.tag_conf.get_unique_images_with_platform(build_dir.platform)[0]
        with tempfile.TemporaryDirectory(dir=build_dir.path) as rpmdb_dir:
            try:
                self.workflow.imageutil.extract_file_from_image(image, RPMDB_PATH, rpmdb_dir)
            except NothingExtractedError:
                if self.workflow.data.dockerfile_images.base_from_scratch:
                    self.log.info("scratch image doesn't contain or has empty rpmdb %s", RPMDB_PATH)
                    return []
                raise

            rpmdb_path = os.path.join(rpmdb_dir, RPMDB_DIR_NAME)

            rpm_cmd = 'rpm --dbpath {} {}'.format(rpmdb_path, rpm_qf_args())
            try:
                self.log.info('getting rpms from rpmdb: %s', rpm_cmd)
                cmd_output = subprocess.check_output(rpm_cmd,
                                                     shell=True, universal_newlines=True)  # nosec
            except Exception as e:
                self.log.error("Failed to get rpms from rpmdb: %s", e)
                raise e
        output = [line for line in cmd_output.splitlines() if line]

        # gpg-pubkey are autogenerated packages by rpm when you import a gpg key
        # these are of course not signed, let's ignore those by default
        if self.ignore_autogenerated_gpg_keys:
            self.log.debug("ignore rpms 'gpg-pubkey'")
            output = [x for x in output if not x.startswith("gpg-pubkey" + self.sep)]

        return parse_rpm_output(output)
