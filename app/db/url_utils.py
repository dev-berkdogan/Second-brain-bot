import re
import urllib.parse

TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "gclsrc", "dclid", "zanpid", "msclkid",
    "igsh", "si", "feature", "ref", "ref_src", "ref_url",
    "_hsenc", "_hsmi", "mc_cid", "mc_eid"
}


def normalize_url(url: str) -> str:
    """
    Platforma duyarlı, deterministik ve izleme parametrelerinden arındırılmış
    kanonik URL döndürür.
    """
    if not url:
        return ""

    raw_url = url.strip()

    if not re.match(r"^https?://", raw_url, re.IGNORECASE):
        raw_url = "https://" + raw_url

    parsed = urllib.parse.urlparse(raw_url)
    hostname = parsed.netloc.lower()
    path = parsed.path.rstrip("/")
    query_params = urllib.parse.parse_qs(
        parsed.query,
        keep_blank_values=False
    )

    # YouTube
    if any(d in hostname for d in ("youtube.com", "youtu.be")):
        video_id = None

        if "youtu.be" in hostname:
            parts = [p for p in path.split("/") if p]
            if parts:
                video_id = parts[0]

        elif any(
            prefix in path
            for prefix in ("/shorts/", "/embed/", "/live/")
        ):
            match = re.search(
                r"/(?:shorts|embed|live)/([A-Za-z0-9_-]+)",
                path
            )
            if match:
                video_id = match.group(1)

        elif "v" in query_params and query_params["v"]:
            video_id = query_params["v"][0]

        else:
            match = re.search(
                r"[?&]v=([A-Za-z0-9_-]+)",
                raw_url
            )
            if match:
                video_id = match.group(1)

        if video_id:
            return f"https://www.youtube.com/watch?v={video_id}"

        return f"https://www.youtube.com{path}"

    # Instagram
    if "instagram.com" in hostname:
        match = re.search(
            r"/(p|reel|tv)/([A-Za-z0-9_-]+)",
            path
        )

        if match:
            media_type, shortcode = match.group(1), match.group(2)
            return (
                f"https://www.instagram.com/"
                f"{media_type}/{shortcode}"
            )

        return f"https://www.instagram.com{path}"

    # TikTok
    if "tiktok.com" in hostname:
        match = re.search(
            r"(/@[^/]+/video/\d+|/v/\d+)",
            path
        )

        if match:
            return f"https://www.tiktok.com{match.group(1)}"

        return f"https://www.tiktok.com{path}"

    # Twitter / X
    if any(d in hostname for d in ("twitter.com", "x.com")):
        match = re.search(
            r"(/[^/]+/status/\d+)",
            path
        )

        if match:
            return f"https://x.com{match.group(1)}"

        return f"https://x.com{path}"

    # LinkedIn
    if "linkedin.com" in hostname:
        match = re.search(
            r"(/posts/[A-Za-z0-9_-]+|/pulse/[A-Za-z0-9_-]+)",
            path
        )

        if match:
            return f"https://www.linkedin.com{match.group(1)}"

        return f"https://www.linkedin.com{path}"

    # Generic Web
    cleaned_query_dict = {
        key: values
        for key, values in query_params.items()
        if key.lower() not in TRACKING_PARAMS
    }

    encoded_query = urllib.parse.urlencode(
        sorted(cleaned_query_dict.items()),
        doseq=True
    )

    scheme = parsed.scheme.lower() or "https"
    netloc = hostname

    if encoded_query:
        return f"{scheme}://{netloc}{path}?{encoded_query}"

    return f"{scheme}://{netloc}{path}"