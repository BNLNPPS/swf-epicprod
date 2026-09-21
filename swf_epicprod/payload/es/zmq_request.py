#!/usr/bin/env python3
"""A request to a running EICrecon on its managed PODIO socket, over
libzmq through ctypes (the image has libzmq for EICrecon and no pyzmq).
REQ/REP over ipc: one JSON request, one JSON reply."""
import ctypes, ctypes.util, json, sys

ZMQ_REQ, ZMQ_RCVTIMEO, ZMQ_SNDTIMEO = 3, 27, 28


def request(socket_path, payload, timeout_s=600):
    lib = ctypes.CDLL(ctypes.util.find_library('zmq') or '/opt/local/lib/libzmq.so')
    lib.zmq_ctx_new.restype = ctypes.c_void_p
    lib.zmq_socket.restype = ctypes.c_void_p
    lib.zmq_socket.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib.zmq_connect.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    lib.zmq_setsockopt.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t]
    lib.zmq_send.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]
    lib.zmq_recv.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]
    lib.zmq_close.argtypes = [ctypes.c_void_p]
    lib.zmq_ctx_term.argtypes = [ctypes.c_void_p]
    ctx = lib.zmq_ctx_new()
    sock = lib.zmq_socket(ctx, ZMQ_REQ)
    ms = ctypes.c_int(int(timeout_s * 1000))
    lib.zmq_setsockopt(sock, ZMQ_RCVTIMEO, ctypes.byref(ms), ctypes.sizeof(ms))
    snd = ctypes.c_int(5000)
    lib.zmq_setsockopt(sock, ZMQ_SNDTIMEO, ctypes.byref(snd), ctypes.sizeof(snd))
    if lib.zmq_connect(sock, f'ipc://{socket_path}'.encode()) != 0:
        raise RuntimeError(f'zmq_connect failed: {socket_path}')
    data = json.dumps(payload).encode()
    if lib.zmq_send(sock, data, len(data), 0) < 0:
        raise RuntimeError('zmq_send failed')
    buf = ctypes.create_string_buffer(1 << 20)
    n = lib.zmq_recv(sock, buf, len(buf), 0)
    lib.zmq_close(sock)
    lib.zmq_ctx_term(ctx)
    if n < 0:
        raise RuntimeError(f'no reply from EICrecon within {timeout_s}s')
    return json.loads(buf.raw[:min(n, len(buf))].decode())


if __name__ == '__main__':
    print(json.dumps(request(sys.argv[1], json.loads(sys.argv[2]))))
