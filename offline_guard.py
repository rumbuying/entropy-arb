
import ipaddress,socket
from pathlib import Path
original_connect=socket.socket.connect
original_connect_ex=socket.socket.connect_ex
original_dns=socket.getaddrinfo
stage=Path.cwd().resolve()
def allowed(address):
 if isinstance(address,tuple):
  try:return ipaddress.ip_address(address[0]).is_loopback
  except ValueError:return address[0]=='localhost'
 if isinstance(address,(str,bytes)):
  path=Path(address.decode() if isinstance(address,bytes) else address).resolve()
  return path.is_relative_to('/tmp') or path.is_relative_to(stage)
 return False
def connect(self,address):
 if not allowed(address):raise PermissionError('OFFLINE_GRID_TEST_NETWORK_DENIED')
 return original_connect(self,address)
def connect_ex(self,address):
 if not allowed(address):raise PermissionError('OFFLINE_GRID_TEST_NETWORK_DENIED')
 return original_connect_ex(self,address)
def dns(host,*args,**kwargs):
 if host not in (None,'localhost'):
  try:okay=ipaddress.ip_address(host).is_loopback
  except ValueError:okay=False
  if not okay:raise PermissionError('OFFLINE_GRID_TEST_DNS_DENIED')
 return original_dns(host,*args,**kwargs)
def pytest_sessionstart(session):
 socket.socket.connect=connect;socket.socket.connect_ex=connect_ex;socket.getaddrinfo=dns
