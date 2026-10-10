"""Optional caller for future backend use. Existing backend code is untouched."""
import httpx

class V6Client:
    def __init__(self,url,api_key,timeout=2):
        self.url=url.rstrip('/')
        self.http=httpx.AsyncClient(timeout=timeout,headers={'Authorization':'Bearer '+api_key},limits=httpx.Limits(max_connections=2,max_keepalive_connections=2))
    async def submit(self,snapshot,idempotency_key,notify=True):
        r=await self.http.post(self.url+'/v1/invocations',params={'mode':'async'},
            headers={'Idempotency-Key':idempotency_key},json={'snapshot':snapshot,'notify':notify,'source':'external_caller'})
        r.raise_for_status();return r.json()
    async def result(self,invocation_id):
        r=await self.http.get(self.url+'/v1/invocations/'+invocation_id)
        r.raise_for_status();return r.json()
    async def close(self):await self.http.aclose()
