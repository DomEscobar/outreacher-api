"""Synthetic parser unit fixtures; actual VNC/WebSocket frames are tested separately."""
from pathlib import Path
import struct
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'runtime'))
from rfb_probe import Reader,handshake_and_frame

class RfbTests(unittest.TestCase):
    def payload(self,version):
        security=b'\0\0\0\x01' if version==3 else b'\x01\x01'+b'\0\0\0\0'
        banner=b'RFB 003.003\n' if version==3 else b'RFB 003.008\n'
        pixel_format=bytes([32,24,0,1])+struct.pack('!HHH',255,255,255)+bytes([16,8,0,0,0,0])
        init=struct.pack('!HH',1,1)+pixel_format+struct.pack('!I',0)
        frame=b'\x00\x00\x00\x01'+struct.pack('!HHHHi',0,0,1,1,0)+b'abcd'
        return banner+security+init+frame

    def test_rfb_33_frame(self):
        chunks=iter([self.payload(3)]); sent=[]
        report=handshake_and_frame(Reader(lambda:next(chunks)),sent.append)
        self.assertEqual(report['frame_bytes_received'],4)
        self.assertEqual(report['rfb'],'003.003')
        self.assertFalse(report['pixel_data_logged'])

    def test_rfb_38_frame(self):
        chunks=iter([self.payload(8)]); sent=[]
        report=handshake_and_frame(Reader(lambda:next(chunks)),sent.append)
        self.assertEqual(report['frame_bytes_received'],4)
        self.assertEqual(report['rfb'],'003.008')

    def test_health_finishes_auth_and_init_without_pixel_request(self):
        chunks=iter([self.payload(8)]); sent=[]
        report=handshake_and_frame(Reader(lambda:next(chunks)),sent.append,want_frame=False)
        self.assertFalse(report['frame_requested'])
        self.assertEqual(sent,[b'RFB 003.008\n',b'\x01',b'\x01'])

    def test_hung_transport_is_not_healthy(self):
        with self.assertRaises(RuntimeError): handshake_and_frame(Reader(lambda:b''),lambda data:None)

if __name__=='__main__': unittest.main()
