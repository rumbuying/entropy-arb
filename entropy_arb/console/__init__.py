"""Console package: web management console for entropy-arb.

Modules:
    secrets     — safe .env reader/writer (masking, validation, audit)
    profiles    — named strategy configs with load_config() validation
    supervisor  — engine worker subprocess lifecycle
    server      — aiohttp app binding it all together (console.py entry)
"""
