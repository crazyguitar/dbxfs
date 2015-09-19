#!/usr/bin/env python3

# This file is part of dropboxfs.

# dropboxfs is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# dropboxfs is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with dropboxfs.  If not, see <http://www.gnu.org/licenses/>.

import os
import logging
import socketserver
import struct
import sys
import time

from datetime import datetime
from io import StringIO

# hack smb struct defs from PySMB
import smb.smb_structs as smb_structs

log = logging.getLogger(__name__)

class SMBMessage(smb_structs.SMBMessage):
    # NB: default _decodePayload() assumes responses from servers
    # since we are the server, we assume requests
    def _decodePayload(self):
        if self.isReply: return super()._decodePayload();

        if self.command == smb_structs.SMB_COM_READ_ANDX:
            self.payload = smb_structs.ComReadAndxRequest()
        elif self.command == smb_structs.SMB_COM_WRITE_ANDX:
            self.payload = smb_structs.ComWriteAndxRequest()
        elif self.command == smb_structs.SMB_COM_TRANSACTION:
            self.payload = smb_structs.ComTransactionRequest()
        elif self.command == smb_structs.SMB_COM_TRANSACTION2:
            self.payload = smb_structs.ComTransaction2Request()
        elif self.command == smb_structs.SMB_COM_OPEN_ANDX:
            self.payload = smb_structs.ComOpenAndxRequest()
        elif self.command == smb_structs.SMB_COM_NT_CREATE_ANDX:
            self.payload = smb_structs.ComNTCreateAndxRequest()
        elif self.command == smb_structs.SMB_COM_TREE_CONNECT_ANDX:
            self.payload = ComTreeConnectAndxRequest()
        elif self.command == smb_structs.SMB_COM_ECHO:
            self.payload = ComEchoRequest()
        elif self.command == smb_structs.SMB_COM_SESSION_SETUP_ANDX:
            self.payload = ComSessionSetupAndxRequest()
        elif self.command == smb_structs.SMB_COM_NEGOTIATE:
            self.payload = ComNegotiateRequest()

        if self.payload:
            self.payload.decode(self)

def init_reply(payload, message, command):
    smb_structs.Payload.initMessage(payload, message)
    message.command = command
    message.flags = message.flags | smb_structs.SMB_FLAGS_REPLY
    message.flags2 = message.flags2 & ~smb_structs.SMB_FLAGS2_EXTENDED_SECURITY
    message.tid = getattr(payload, 'tid', 0)
    message.uid = getattr(payload, 'uid', 0)
    message.mid = getattr(payload, 'mid', 0)

def prepare(payload, message):
    assert message.payload is payload
    message.pid = getattr(payload, 'pid', 0)

class ComNegotiateRequest(smb_structs.ComNegotiateRequest):
    def __str__(self):
        lines = []

        lines.append("SMB_COM_NEGOTIATE (request)")
        lines.append("Dialect Supported:")
        for d in self.dialects:
            lines.append("  %s" % (d,))

        return os.linesep.join(lines)

    def decode(self, message):
        self.dialects = message.data.split(b'\0')
        a = self.dialects.pop()
        if a: raise smb_structs.ProtocolError("Non-trailing null byte!")
        self.dialects = [d.lstrip(b"\x02").decode("ascii") for d in self.dialects]

class ComNegotiateResponse(smb_structs.ComNegotiateResponse):
    def __init__(self, **kw):
        for (k, v) in kw.items():
            setattr(self, k, v)

    def initMessage(self, message):
        init_reply(self, message, smb_structs.SMB_COM_NEGOTIATE)

    def prepare(self, message):
        prepare(self, message)

        message.parameters_data = struct.pack(self.PAYLOAD_STRUCT_FORMAT,
                                              self.dialect_index, self.security_mode, self.max_mpx_count,
                                              self.max_number_vcs, self.max_buffer_size, self.max_raw_size,
                                              self.session_key, self.capabilities, self.system_time,
                                              self.server_time_zone, self.challenge_length)
        message.data = b''

class ComSessionSetupAndxRequest(smb_structs.ComSessionSetupAndxRequest__NoSecurityExtension):
    def __init__(self):
        pass

    def decode(self, message):
        andx_header = message.parameters_data[:self.DEFAULT_ANDX_PARAM_SIZE]
        (andx_command, andx_reserved, andx_offset) = andx_header_o = struct.unpack(">BBH", andx_header)

        # TODO: better andx parsing
        if not (andx_command == 0xff and not andx_offset):
            raise Exception("We don't support non-terminal ANDX parameter blocks yet...")

        params = message.parameters_data[self.DEFAULT_ANDX_PARAM_SIZE:]
        (max_buffer_size, max_mpx_count, vc_number,
         session_key, length1, length2, reserved, capabilities) = params_o =struct.unpack(self.PAYLOAD_STRUCT_FORMAT, params)


        is_unicode = message.flags2 & smb_structs.SMB_FLAGS2_UNICODE

        case_insensitive_password = message.data[:length1].rstrip(b'\0').decode("ascii")
        case_sensitive_password = message.data[length1:length1 + length2].rstrip(b'\0').decode("ascii")

        # read padding
        raw_offset = (SMBMessage.HEADER_STRUCT_SIZE + len(message.parameters_data) + 2 +
                      length1 + length2)
        if raw_offset % 2 and is_unicode:
            if message.raw_data[raw_offset] != 0:
                raise Exception("Was expecting null byte!: %r" % (message.raw_data[raw_offset],))
            raw_offset += 1

        term = b"\0\0" if is_unicode else b"\0"
        encoding = "utf-16-le" if is_unicode else "ascii"

        elts = {}
        for n in ["username", "domain", "nativeos", "nativelanman"]:
            s = raw_offset
            while True:
                next_offset = message.raw_data.index(term, s)
                if next_offset % 2: s = next_offset + 1
                else: break
            elts[n] = message.raw_data[raw_offset:next_offset].decode(encoding)
            raw_offset = next_offset + len(term)

        self.max_buffer_size = max_buffer_size
        self.max_mpx_count = max_mpx_count
        self.vc_number = vc_number
        self.session_key = session_key
        self.capabilities = capabilities

        self.password = (case_insensitive_password or case_sensitive_password)
        self.username = elts['username']
        self.domain = elts['domain']
        self.native_os = elts['nativeos']
        self.native_lan_man = elts['nativelanman']

class ComSessionSetupAndxResponse(smb_structs.ComSessionSetupAndxResponse):
    def __init__(self, **kw):
        for (k, v) in kw.items():
            setattr(self, k, v)

    def initMessage(self, message):
        init_reply(self, message, smb_structs.SMB_COM_SESSION_SETUP_ANDX)

    def prepare(self, message):
        prepare(self, message)

        security_blob = b''

        # this gets reset in SMBMessage.encode()
        message.pid = self.pid

        message.parameters_data = (self.DEFAULT_ANDX_PARAM_HEADER +
                                   struct.pack('<HH',self.action, len(security_blob)))

        prefix = b''
        if (SMBMessage.HEADER_STRUCT_SIZE + len(message.parameters_data) +
            len(security_blob)) % 2:
            prefix = b'\0'

        message.data = security_blob + prefix + b''.join([x.encode("utf-16-le") + b'\0\0'  for x in ["Unix", "DropboxFS", self.domain]])

class ComTreeConnectAndxRequest(smb_structs.ComTreeConnectAndxRequest):
    def __init__(self): pass

    def decode(self, message):
        andx_header = message.parameters_data[:self.DEFAULT_ANDX_PARAM_SIZE]
        (andx_command, andx_reserved, andx_offset) = andx_header_o = struct.unpack(">BBH", andx_header)

        # TODO: better andx parsing
        if not (andx_command == 0xff and not andx_offset):
            raise Exception("We don't support non-terminal ANDX parameter blocks yet...")

        (flags, password_len) = struct.unpack(self.PAYLOAD_STRUCT_FORMAT,
                                              message.parameters_data[self.DEFAULT_ANDX_PARAM_SIZE:self.PAYLOAD_STRUCT_SIZE + self.DEFAULT_ANDX_PARAM_SIZE])

        self.password = message.data[:password_len].rstrip(b'\0').decode("utf-8")

        offset = password_len
        if (SMBMessage.HEADER_STRUCT_SIZE + len(message.parameters_data) +
            password_len) % 2:
            if message.data[password_len] != 0:
                raise Exception("Was expecting null byte padding!")
            offset += 1

        s = offset
        while True:
            share_name_offset = message.data.index(b'\0\0', s)
            if (share_name_offset + SMBMessage.HEADER_STRUCT_SIZE +
                len(message.parameters_data)) % 2:
                s = share_name_offset + 1
            else: break
        self.path = message.data[offset:share_name_offset].decode('utf-16-le')

        service_off = message.data.index(b'\0', share_name_offset + 2)
        self.service = message.data[share_name_offset + 2:service_off].decode("ascii")

class ComTreeConnectAndxResponse(smb_structs.ComTreeConnectAndxResponse):
    def __init__(self, **kw):
        for (k, v) in kw.items():
            setattr(self, k, v)

    def initMessage(self, message):
        init_reply(self, message, smb_structs.SMB_COM_TREE_CONNECT_ANDX)

    def prepare(self, message):
        prepare(self, message)

        self.parameters_data = struct.pack(self.PAYLOAD_STRUCT_FORMAT,
                                           0xff, 0, 0,
                                           self.optional_support)

        # NB A: means disk share
        self.data = b'A:\0'

class ComEchoRequest(smb_structs.ComEchoRequest):
    def decode(self, message):
        fmt = '<H'
        fmt_size = struct.calcsize(fmt)
        (self.echo_count,) = struct.unpack(fmt, message.parameters_data[:fmt_size])
        self.echo_data = message.data

class ComEchoResponse(smb_structs.ComEchoResponse):
    def __init__(self, **kw):
        for (k, v) in kw.items():
            setattr(self, k, v)

    def initMessage(self, message):
        init_reply(self, message, smb_structs.SMB_COM_ECHO)

    def prepare(self, message):
        prepare(self, message)

        message.parameters_data = struct.pack("<H", self.sequence_number)
        message.data = self.data

def response_args_from_req(req, **kw):
    return dict(pid=req.pid, tid=req.tid,
                uid=req.uid, mid=req.mid, **kw)

STATUS_NOT_FOUND = 0xc0000225

class NullPayload(smb_structs.Payload):
    def __init__(self, **kw):
        for (k, v) in kw.items():
            setattr(self, k, v)

    def initMessage(self, message):
        init_reply(self, message, self.command)

    def prepare(self, message):
        prepare(self, message)

def error_response(req, status):
    assert status, "Status must be an error!"
    m = SMBMessage(NullPayload(**response_args_from_req(req, command=req.command)))
    m.message.status.internal_value = status
    m.message.status.is_ntstatus = True
    return m

def decode_smb_message(message):
    i = SMBMessage()
    i.decode(message)
    return i

def recv_all(sock, len_):
    toret = []
    recvd = 0
    while recvd != len_:
        b = sock.recv(len_ - recvd)
        if not b:
            raise Exception("EOF while expecting data!")
        recvd += len(b)
        toret.append(b)
    return b''.join(toret)

class SMBClientHandler(socketserver.BaseRequestHandler):
    def read_message(self):
        data = self.request.recv(4)
        (length,) = struct.unpack(">I", data)
        return decode_smb_message(recv_all(self.request, length))

    def send_message(self, msg):
        msg.raw_data = msg.encode()
        self.request.sendall(struct.pack(">I", len(msg.raw_data)) + msg.raw_data)

    def handle(self):
        negotiate_req = self.read_message()
        if negotiate_req.command != smb_structs.SMB_COM_NEGOTIATE:
            raise Exception("Got unexpected request: %s" % (negotiate_req,))

        server_capabilities = (smb_structs.CAP_UNICODE |
                               smb_structs.CAP_LARGE_FILES |
                               smb_structs.CAP_STATUS32)

        # win32 time
        ts = time.time()
        win32_time = (int(ts) + 11644473600) * 10000000
        utc_offset = int(-(datetime.fromtimestamp(ts) -
                           datetime.utcfromtimestamp(ts)).total_seconds() / 60)
        args = dict(
            # TODO: catch this and throw a friendlier error
            dialect_index=negotiate_req.payload.dialects.index('NT LM 0.12'),
            security_mode=0, # we support NO SECURITY FEATURES
            max_mpx_count=2 ** 16 - 1, # unlimited clients baby
            max_number_vcs=2 ** 16 - 1, # not sure what this means
            max_buffer_size=2 ** 16 - 1, # this doesn't matter
            max_raw_size=2 ** 16 - 1, # this doesn't matter
            session_key=0, # can be anything, we don't use it
            capabilities=server_capabilities,
            system_time=win32_time,
            server_time_zone=utc_offset,
            challenge_length=0,
        )

        negotiate_resp = smb_structs.SMBMessage(ComNegotiateResponse(**args))
        # TODO: set flags? status?

        self.send_message(negotiate_resp)

        session_setup_andx_req = self.read_message()
        if session_setup_andx_req.command != smb_structs.SMB_COM_SESSION_SETUP_ANDX:
            raise Exception("Got unexpected request: %s" % (session_setup_andx_req,))

        if session_setup_andx_req.payload.capabilities & ~server_capabilities:
            raise Exception("Client sent capabilities outside of the server posted caps")

        args = response_args_from_req(session_setup_andx_req,
                                      action=1,
                                      domain=session_setup_andx_req.payload.domain)
        session_setup_andx_resp = SMBMessage(ComSessionSetupAndxResponse(**args))
        self.send_message(session_setup_andx_resp)

        tree_connect_andx_req = self.read_message()
        if tree_connect_andx_req.command != smb_structs.SMB_COM_TREE_CONNECT_ANDX:
            raise Exception("Got unexpected request: %s" % (session_setup_andx_req,))

        if tree_connect_andx_req.payload.service not in ("?????", "A:"):
            raise Exception("We don't provide the requested service: %s" %
                            (tree_connect_andx_req.payload.service,))

        args = response_args_from_req(tree_connect_andx_req,
                                      optional_support=smb_structs.SMB_TREE_CONNECTX_SUPPORT_SEARCH,
                                      service="A:",
                                      native_file_system="FAT")
        self.send_message(SMBMessage(ComTreeConnectAndxResponse(**args)))

        echo_req = self.read_message()
        if echo_req.payload.echo_count > 1:
            raise Exception("Echo count is too high: %r" % (req.payload.echo_count,))

        args = response_args_from_req(echo_req,
                                      sequence_number=0,
                                      data=echo_req.payload.echo_data)
        self.send_message(SMBMessage(ComEchoResponse(**args)))

def main(argv):
    logging.basicConfig(level=logging.DEBUG)

    # run basic server
    server = socketserver.ThreadingTCPServer(('localhost', 8888),
                                             SMBClientHandler, False)
    server.allow_reuse_address = True
    server.server_bind()
    server.server_activate()
    server.serve_forever()
    return 0

if __name__ == "__main__":
    sys.exit(main(sys.argv))
