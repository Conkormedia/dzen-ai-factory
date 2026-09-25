"""Dzen Autopilot: brief-driven SEO article factory with automatic Dzen publishing.

One long-running service (see ``autopilot.main``) that:

* keeps a bank of article topics per promoted project (business brief),
* writes SEO articles with an LLM, checks them deterministically,
* generates a cover image,
* publishes to Dzen through the editor API inside a persistent browser session,
* reports to Telegram (compact per-article lines + a daily digest).
"""

__version__ = "0.1.0"
