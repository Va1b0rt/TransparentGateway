#!/usr/bin/python3
"""Run on isolated client: one plain TCP/HTTP request, no proxy environment or credentials."""
import json
import socket
import subprocess
import uuid


def main():
    routes=json.loads(subprocess.check_output(['ip','-j','-4','route','show','default']))
    assert len(routes)==1 and routes[0]['dev']=='eth1' and routes[0]['gateway']=='192.168.91.1'
    target='172.30.40.10'
    route=json.loads(subprocess.check_output(['ip','-j','-4','route','get',target]))[0]
    assert route['dev']=='eth1' and route['gateway']=='192.168.91.1' and route['prefsrc']=='192.168.91.2'
    request_id=uuid.uuid4().hex
    with socket.create_connection((target,8000),8) as stream:
        stream.settimeout(10)
        assert stream.getsockname()[0]=='192.168.91.2'
        stream.sendall(('GET / HTTP/1.1\r\nHost: '+target+':8000\r\nX-Request-ID: '+request_id+'\r\nConnection: close\r\n\r\n').encode())
        response=stream.makefile('rb')
        assert b' 200 ' in response.readline(4096)
        headers={}
        while True:
            line=response.readline(4096)
            if line==b'\r\n': break
            assert line
            key,value=line.decode().split(':',1)
            headers[key.lower()]=value.strip()
        length=int(headers['content-length'])
        assert 0<length<=65536
        data=json.loads(response.read(length))
    echoed={k.lower():v.strip() for k,v in data['headers'].items()}
    assert echoed.get('x-request-id')==request_id
    assert data['backend_peer']=='172.30.40.11'
    assert not any(key in echoed for key in ['proxy-authorization','forwarded','x-forwarded-for','x-real-ip'])
    print(json.dumps({'client':socket.gethostname(),'source':'192.168.91.2','gateway':'192.168.91.1',
                      'peer':data['backend_peer'],'request_id':request_id,'transparent_tcp_echo':'PASS'}))


if __name__=='__main__':
    try: main()
    except Exception as error:
        print('CLIENT_PROBE FAILED: '+type(error).__name__)
        raise SystemExit(1)
