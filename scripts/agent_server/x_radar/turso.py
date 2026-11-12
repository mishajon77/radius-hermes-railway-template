"""SQLite statements over Turso's documented HTTP database protocol."""
from urllib.parse import urlsplit

import httpx

from .store import RadarError


def statement(sql, params):
    args = [
        {"type": "integer", "value": str(value)} if isinstance(value, int)
        else {"type": "text", "value": value}
        for value in params
    ]
    return {"sql": sql, "args": args}


def value(cell):
    if cell["type"] == "null":
        return None
    if cell["type"] == "integer":
        return int(cell["value"])
    return cell["value"]


class TursoDatabase:
    def __init__(self, url, token, transport=None):
        parsed = urlsplit(url)
        if (parsed.scheme not in {"libsql", "https"} or not parsed.hostname
                or not parsed.hostname.endswith(".turso.io") or parsed.username or parsed.password
                or parsed.port or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
                or not token or not token.isascii() or any(char.isspace() for char in token)):
            raise ValueError("A Turso database URL and database token are required")
        self.url = f"https://{parsed.hostname}/v2/pipeline"
        self.client = httpx.Client(
            headers={"Authorization": f"Bearer {token}"},
            timeout=httpx.Timeout(8, connect=4), follow_redirects=False, transport=transport,
        )

    def run(self, commands, atomic=False):
        if atomic:
            steps = [{"stmt": statement("BEGIN IMMEDIATE", ())}]
            for sql, params in [*commands, ("COMMIT", ())]:
                steps.append({
                    "condition": {"type": "ok", "step": len(steps) - 1},
                    "stmt": statement(sql, params),
                })
            requests = [{"type": "batch", "batch": {"steps": steps}}]
        else:
            requests = [{"type": "execute", "stmt": statement(sql, params)} for sql, params in commands]
        # Closing the stream also rolls back if a failed step prevented COMMIT.
        requests.append({"type": "close"})
        try:
            response = self.client.post(self.url, json={"requests": requests})
            response.raise_for_status()
            results = response.json()["results"]
            if len(results) != len(requests) or any(item["type"] != "ok" for item in results):
                raise ValueError
            if atomic:
                batch = results[0]["response"]["result"]
                steps = batch["step_results"]
                if len(steps) != len(commands) + 2 or any(batch["step_errors"]) or steps[-1] is None:
                    raise ValueError
                records = steps[1:-1]
            else:
                records = [item["response"]["result"] for item in results[:-1]]
            return [
                ([tuple(value(cell) for cell in row) for row in record["rows"]], record["affected_row_count"])
                for record in records
            ]
        except (httpx.HTTPError, KeyError, TypeError, ValueError, IndexError) as exc:
            raise RadarError("ارتباط با دیتابیس Turso برقرار نشد. آدرس و توکن دیتابیس را بررسی کن.", 503) from exc

    def close(self):
        self.client.close()
