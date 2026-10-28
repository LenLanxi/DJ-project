"""Scripted byte-channel process for container supervisor tests."""
import asyncio
import json


class Pipe:
    def __init__(self): self.data=bytearray()
    def write(self,data): self.data.extend(data)
    async def drain(self): pass


class Process:
    def __init__(self,messages=(),stderr=b''):
        self.stdin=Pipe(); self.stdout=asyncio.StreamReader(); self.stderr=asyncio.StreamReader()
        for m in messages: self.stdout.feed_data(json.dumps(m).encode()+b'\n')
        self.stdout.feed_eof(); self.stderr.feed_data(stderr); self.stderr.feed_eof()
        self.returncode=None
    async def wait(self): self.returncode=0; return 0
    def kill(self): self.returncode=-9
