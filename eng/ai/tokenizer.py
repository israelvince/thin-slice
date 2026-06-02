try:
    import tiktoken
except Exception:
    tiktoken = None

def estimate_tokens(text: str) -> int:
    """Estimate tokens for text. Use tiktoken if available, otherwise fallback to word heuristic."""
    if tiktoken is not None:
        try:
            enc = tiktoken.get_encoding("cl100k_base")
            return len(enc.encode(text))
        except Exception:
            pass
    # fallback heuristic: words * 1.3
    words = len(text.split())
    return max(1, int(words * 1.3))
