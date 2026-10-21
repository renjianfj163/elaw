import httpx

HEADERS = {
    "User-Agent": "elaw-compliance-checker/0.1 (local single-user verifier)",
    "Referer": "https://flk.npc.gov.cn/",
    "Accept": "application/json,text/html;q=0.9",
}


class HttpxClient:
    def __init__(self) -> None:
        self._client = httpx.Client(timeout=25, follow_redirects=True, headers=HEADERS)

    def post_json(self, url: str, payload) -> dict:
        response = self._client.post(url, json=payload)
        response.raise_for_status()
        return response.json()

    def get_bytes(self, url: str) -> bytes:
        response = self._client.get(url)
        response.raise_for_status()
        return response.content

    def get_text(self, url: str, params: dict | None = None) -> str:
        response = self._client.get(url, params=params)
        response.raise_for_status()
        response.encoding = response.encoding or "utf-8"
        return response.text
