import json
import subprocess
import time


def run_mcp_call(tool, arguments):
    process = subprocess.Popen(
        ["python", "src/mcp_dblp/server.py"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    time.sleep(0.5)

    message = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "call_tool",
        "params": {"name": tool, "arguments": arguments},
    }

    json_content = json.dumps(message)
    content_length = len(json_content)
    request = f"Content-Length: {content_length}\r\n\r\n{json_content}"

    try:
        process.stdin.write(request)
        process.stdin.flush()

        headers = {}
        while True:
            line = process.stdout.readline().strip()
            if not line:
                break
            parts = line.split(":", 1)
            if len(parts) == 2:
                headers[parts[0].strip().lower()] = parts[1].strip()

        content_length = int(headers.get("content-length", 0))
        process.stdout.read(content_length) if content_length > 0 else ""

    except Exception:
        pass
    finally:
        process.terminate()


if __name__ == "__main__":
    run_mcp_call("echo", {})
