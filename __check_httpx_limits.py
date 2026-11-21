import httpx
import httpcore
limits = httpx.Limits()
print(f"httpx version: {httpx.__version__}")
print(f"httpcore version: {httpcore.__version__}")
print(f"default max_keepalive_connections: {limits.max_keepalive_connections}")
print(f"default max_connections: {limits.max_connections}")
print(f"default keepalive_expiry: {limits.keepalive_expiry}")

# Check what AsyncOpenAI passes to httpx by default
import openai
import inspect
src = inspect.getsource(openai.AsyncOpenAI.__init__)
# Just print the httpx-related parts
for line in src.splitlines():
    if any(k in line for k in ['httpx', 'limits', 'keepalive', 'http_client', 'AsyncClient']):
        print(f"  openai init: {line.strip()}")
