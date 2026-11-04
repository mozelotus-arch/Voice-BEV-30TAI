"""Small local operator client. Does not automate a browser or contact board devices."""
import argparse
import json
import re
from urllib.request import Request, urlopen


class Client:
    def __init__(self, port=8883):
        self.base = 'http://127.0.0.1:' + str(port)
        with urlopen(self.base, timeout=5) as response:
            self.token = re.search(r'name="control-token" content="([^"]+)"', response.read().decode()).group(1)

    def status(self):
        with urlopen(self.base + '/api/status', timeout=5) as response:
            return json.load(response)

    def post(self, path, **kwargs):
        s = self.status()
        body = dict(instance_id=s['instance_id'], epoch=s['epoch'], **kwargs)
        req = Request(self.base + path, json.dumps(body, ensure_ascii=False).encode(),
                      {'Content-Type':'application/json', 'X-Control-Token':self.token})
        with urlopen(req, timeout=10) as response:
            return json.load(response)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('status','shutdown'))
    args = parser.parse_args()
    client = Client()
    print(json.dumps(client.status() if args.action == 'status' else client.post('/api/service/shutdown'), ensure_ascii=False, indent=2))
