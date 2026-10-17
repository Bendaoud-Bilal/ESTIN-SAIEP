"""Local CLI state (~/.saiep/state.json): provider, cluster name, deployed projects."""
import json
import os

from . import STATE_DIR

DEFAULT = {'provider': 'kind', 'cluster': 'saiep', 'projects': {}}


class State:
    def __init__(self, directory=STATE_DIR):
        self.directory = directory
        self.path = os.path.join(directory, 'state.json')
        self.data = dict(DEFAULT, projects={})
        if os.path.exists(self.path):
            with open(self.path) as f:
                self.data.update(json.load(f))

    def save(self):
        os.makedirs(self.directory, mode=0o700, exist_ok=True)
        tmp = self.path + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(self.data, f, indent=2)
        os.replace(tmp, self.path)

    def __getitem__(self, key):
        return self.data[key]

    def __setitem__(self, key, value):
        self.data[key] = value
