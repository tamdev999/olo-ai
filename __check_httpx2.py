import httpx2
l = httpx2.Limits()
print(f"httpx2 version: {httpx2.__version__}")
print(f"httpx2 max_keepalive_connections: {l.max_keepalive_connections}")
print(f"httpx2 max_connections: {l.max_connections}")
print(f"httpx2 keepalive_expiry: {l.keepalive_expiry}")

# Check openai's default http client setup
import openai._base_client as bc
import inspect
src = inspect.getsource(bc)
for i, line in enumerate(src.splitlines()):
    if any(k in line for k in ['Limits', 'keepalive', 'max_keepalive', 'max_connections', 'AsyncClient']):
        print(f"  base_client:{i}: {line.strip()}")
