"""RFB health/transport probe. Report protocol/counts only, never pixels/clipboard."""
import argparse
import json
import socket
import struct

class Reader:
    def __init__(self,receive): self.receive=receive; self.buffer=b''
    def take(self,count):
        if count>32*1024*1024: raise RuntimeError('Unexpected RFB payload size')
        while len(self.buffer)<count:
            block=self.receive()
            if not isinstance(block,bytes) or not block: raise RuntimeError('RFB transport ended or non-binary payload')
            self.buffer+=block
        result=self.buffer[:count]; self.buffer=self.buffer[count:]
        return result


def handshake_and_frame(reader,send,want_frame=True):
    banner=reader.take(12)
    if banner not in (b'RFB 003.003\n',b'RFB 003.008\n'): raise RuntimeError('Unexpected RFB banner')
    send(banner)
    if banner==b'RFB 003.003\n':
        if reader.take(4)!=b'\0\0\0\x01': raise RuntimeError('Private RFB 3.3 backend did not offer None security')
    else:
        count=reader.take(1)[0]
        if count==0: raise RuntimeError('RFB server rejected negotiation')
        types=reader.take(count)
        if 1 not in types: raise RuntimeError('Private VNC backend did not offer expected None security')
        send(b'\x01')
        if reader.take(4)!=b'\0\0\0\0': raise RuntimeError('RFB security negotiation failed')
    send(b'\x01') # Shared client: never evict the human viewer.
    header=reader.take(24)
    width,height=struct.unpack('!HH',header[:4])
    bpp=header[4]
    if not 1<=width<=8192 or not 1<=height<=8192 or bpp not in (8,16,32): raise RuntimeError('Unexpected framebuffer dimensions')
    name_length=struct.unpack('!I',header[20:24])[0]
    reader.take(name_length) # Server name is intentionally not logged.
    if not want_frame:
        return {'rfb':banner[4:11].decode(),'width':width,'height':height,'frame_requested':False}
    send(b'\x02\x00\x00\x01'+struct.pack('!i',0)) # Raw encoding only.
    send(b'\x03\x00'+struct.pack('!HHHH',0,0,width,height))
    if reader.take(1)!=b'\x00': raise RuntimeError('Unexpected server message; no clipboard inspection')
    padding_and_count=reader.take(3)
    rectangles=struct.unpack('!H',padding_and_count[1:])[0]
    if not rectangles or rectangles>4096: raise RuntimeError('Invalid framebuffer update')
    received=0
    for _ in range(rectangles):
        x,y,w,h,encoding=struct.unpack('!HHHHi',reader.take(12))
        if encoding!=0 or x+w>width or y+h>height: raise RuntimeError('Unexpected rectangle encoding/bounds')
        length=w*h*(bpp//8)
        # Only counts survive; pixel payload is discarded and never displayed.
        reader.take(length)
        received+=length
    if not received: raise RuntimeError('No framebuffer data')
    return {'rfb':banner[4:11].decode(),'width':width,'height':height,'rectangles':rectangles,'frame_bytes_received':received,'pixel_data_logged':False}


def probe_websocket():
    from websockets.sync.client import connect
    with connect('ws://127.0.0.1:6080/websockify',origin='https://localhost',subprotocols=['binary'],open_timeout=5,max_size=32*1024*1024,compression=None) as ws:
        return handshake_and_frame(Reader(lambda:ws.recv(timeout=10)),ws.send)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--websocket',action='store_true')
    args=parser.parse_args()
    if args.websocket: result=probe_websocket()
    else:
        with socket.create_connection(('127.0.0.1',5900),timeout=5) as sock:
            sock.settimeout(10)
            result=handshake_and_frame(Reader(lambda:sock.recv(65536)),sock.sendall)
    print(json.dumps(result,indent=2))

if __name__=='__main__': main()
