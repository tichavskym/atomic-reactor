"""
Reads input from provided path
"""
import json
from dock.constants import CONTAINER_BUILD_JSON_PATH

from dock.plugin import InputPlugin


class PathInputPlugin(InputPlugin):
    key = "path"

    def __init__(self, path=None):
        """
        constructor
        """
        # call parent constructor
        super(PathInputPlugin, self).__init__()
        self.path = path

    def run(self):
        """
        get json with build config from path
        """
        path = self.path or CONTAINER_BUILD_JSON_PATH
        try:
            with open(path, 'r') as build_cfg_fd:
                build_cfg_json = json.load(build_cfg_fd)
        except ValueError:
            self.log.error("couldn't decode json from file '%s'", path)
            return None
        except IOError:
            self.log.error("couldn't read json from file '%s'", path)
            return None
        else:
            return build_cfg_json
