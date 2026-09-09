#!/usr/bin/python3
"""Fresh DNS, TLS/public and local echo from an isolated lab client; no proxy credentials."""
import http.client
import ipaddress
import json
import socket
import ssl
import struct
import subprocess
import uuid


def main():
    routes=json.loads(subprocess.check_output(['ip','-j','-4','route','show','default']))
    assert len(routes)==1 and routes[0]['dev']=='eth1' and routes[0]['gateway']=='192.168.91.1'
    assert not json.loads(subprocess.check_output(['ip','-j','-6','route','show','default']))
    ident=uuid.uuid4().int & 65535
    domain='echo.devdays.net.ua'
    query=struct.pack('!6H',ident,0x0100,1,0,0,0)+b''.join(bytes([len(x)])+x.encode() for x in domain.split('.'))+b'\0'+struct.pack('!HH',1,1)
    with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as udp:
        udp.settimeout(20);udp.connect(('1.1.1.1',53))
        assert udp.getsockname()[0]=='192.168.91.2'
        udp.send(query);packet=udp.recv(65535)
    identifier,flags,questions,answers,_,_=struct.unpack('!6H',packet[:12])
    assert identifier==ident and flags & 0x8000 and flags & 15==0 and questions==1
    def skip(offset):
        for _ in range(128):
            size=packet[offset]
            if size & 0xc0==0xc0:return offset+2
            offset+=1
            if size==0:return offset
            assert size<64
            offset+=size
        raise ValueError('DNS name too long')
    cursor=skip(12)+4;addresses=[]
    for _ in range(answers):
        cursor=skip(cursor)
        kind,cls,ttl,size=struct.unpack('!HHIH',packet[cursor:cursor+10]);cursor+=10
        if kind==1 and cls==1 and size==4:addresses.append(socket.inet_ntoa(packet[cursor:cursor+4]))
        cursor+=size
    assert addresses and all(ipaddress.ip_address(a).is_global for a in addresses)
    def echo(address,port,tls):
        marker=uuid.uuid4().hex
        stream=socket.create_connection((address,port),20)
        stream.settimeout(20)
        assert stream.getsockname()[0]=='192.168.91.2'
        try:
            if tls:stream=ssl.create_default_context().wrap_socket(stream,server_hostname=domain)
            host=domain if tls else address+':'+str(port)
            stream.sendall(('GET / HTTP/1.1\r\nHost: '+host+'\r\nUser-Agent: TG-Client-Probe/1.0\r\nX-Request-ID: '+marker+'\r\nConnection: close\r\n\r\n').encode())
            response=http.client.HTTPResponse(stream);response.begin();assert response.status==200
            body=response.read(65537);assert len(body)<=65536
            data=json.loads(body);headers={k.lower():v.strip() for k,v in data['headers'].items()}
            assert headers.get('x-request-id')==marker
            assert not any(k in headers for k in ('authorization','proxy-authorization','cookie'))
            if not tls:assert not any(k in headers for k in ('forwarded','x-forwarded-for','x-real-ip'))
            return data['backend_peer'],marker
        finally:stream.close()
    public,public_id=echo(addresses[0],443,True)
    local,local_id=echo('172.30.40.10',8000,False)
    assert local=='172.30.40.11' and ipaddress.ip_address(public).is_global
    # Non-DNS datagrams must hit the gateway's persistent forwarding guard.
    with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as udp:
        for _ in range(3):udp.sendto(b'TG-DROP-CHECK',('198.51.100.1',44444))
    print(json.dumps({'public_exit':public,'public_request_id':public_id,'local_exit':local,
        'local_request_id':local_id,'dns':'PASS','tls':'PASS','client_route':'PASS','drop_probe_sent':True}))


if __name__=='__main__':
    try:main()
    except Exception as error:
        print('CLIENT_PUBLIC_PROBE FAILED: '+type(error).__name__+'; addresses hidden')
        raise SystemExit(1)
