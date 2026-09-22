#!/usr/bin/env python3
"""LinkedIn Official REST API /rest/posts Publisher.

Publishes structured text posts directly to LinkedIn via official OAuth 2.0 access token.
Uses HTTP/1.1 and IPv4 for optimal mobile network compatibility.
"""

import os
import sys
import json
import argparse
import subprocess
from typing import Optional, Dict, Any

DEFAULT_TOKEN = os.environ.get("LINKEDIN_ACCESS_TOKEN", "AQX_TX2Imfvpyypc-bmecHxNrTdTl4onYENbrwKOL8QFzjASkcdj4f2I0o0F7GYQ1E8W7PdS-rBO7eYXIkYtyzTfRZhGXRyr1IMw7YjmEf7_LWk3l2ySG3wfBYVVHXs5R-UrT9lmQa2bDnyzFNX_l5gHZolD-qJ_OKBN8QB2V_GYMkxSz9DLxvKHk048XrudEQi_8eLfbRRK0widCPBgXW37pCYtnssMa6I00HGiNa7nuHSJE1FzmfmduD_YQZbEYYrBLd0g-Zh4hwPJFrZHaBC4VJftUhiTdfjsE_iAssgsfsmTyp6TOn0U6oRw6PjAxPtzOYgubqEWgArjiehcHHVlzmMxyg")
DEFAULT_AUTHOR = os.environ.get("LINKEDIN_AUTHOR_URN", "urn:li:person:xmO2taL19I")


def publish_post(
    text: str,
    access_token: str = DEFAULT_TOKEN,
    author_urn: str = DEFAULT_AUTHOR,
    visibility: str = "PUBLIC",
) -> Dict[str, Any]:
    """Publish a post to LinkedIn using curl with IPv4 and HTTP/1.1."""
    payload = {
        "author": author_urn,
        "commentary": text,
        "visibility": visibility,
        "distribution": {
            "feedDistribution": "MAIN_FEED",
            "targetEntities": [],
            "thirdPartyDistributionChannels": []
        },
        "lifecycleState": "PUBLISHED"
    }

    import tempfile
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".json") as tf:
        json.dump(payload, tf)
        tmp_path = tf.name

    cmd = [
        "curl", "-s", "-4", "--http1.1", "-X", "POST",
        "https://api.linkedin.com/rest/posts",
        "-H", f"Authorization: Bearer {access_token}",
        "-H", "Content-Type: application/json",
        "-H", "LinkedIn-Version: 202503",
        "-H", "X-Restli-Protocol-Version: 2.0.0",
        "-i",
        "--data-binary", f"@{tmp_path}"
    ]

    res = subprocess.run(cmd, capture_output=True, text=True)
    os.remove(tmp_path)

    if "201 Created" in res.stdout:
        # Extract post urn
        post_urn = "unknown"
        for line in res.stdout.splitlines():
            if line.lower().startswith("x-restli-id:"):
                post_urn = line.split(":", 1)[1].strip()
        return {"ok": True, "post_urn": post_urn, "message": "Post published successfully to LinkedIn!"}
    else:
        return {"ok": False, "raw_response": res.stdout, "error": res.stderr}


def main():
    parser = argparse.ArgumentParser(description="Publish post to LinkedIn")
    parser.add_argument("--token", default=DEFAULT_TOKEN, help="OAuth access token")
    parser.add_argument("--author", default=DEFAULT_AUTHOR, help="Author URN")
    parser.add_argument("--file", help="Path to text file containing post content")
    parser.add_argument("--text", help="Post text inline")
    args = parser.parse_args()

    post_text = args.text
    if args.file:
        with open(args.file, "r") as f:
            post_text = f.read()

    if not post_text:
        print("ERROR: No post text provided.")
        sys.exit(1)

    result = publish_post(post_text, args.token, args.author)
    print(json.dumps(result, indent=2))
    sys.exit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
