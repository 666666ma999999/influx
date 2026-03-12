"""
Hook point definitions for the influx extension architecture.

Each tier corresponds to a pipeline stage:
  - TIER1: Collection & ingestion
  - TIER2: Classification & training
  - TIER3: Curation & publishing

Hook entries carry:
  - name: unique dot-separated identifier
  - description: what the hook does
  - payload_contract: JSON schema $id reference (or None if untyped)
"""

# ---------------------------------------------------------------------------
# Tier 1 -- Collection & Ingestion
# ---------------------------------------------------------------------------
TIER1_HOOKS = {
    "collect.pre": {
        "name": "collect.pre",
        "description": "Fired before a collection run starts. "
                       "Extensions can modify collection parameters (groups, scroll count, date range).",
        "payload_contract": None,
    },
    "collect.source": {
        "name": "collect.source",
        "description": "Fired for each source adapter invocation. "
                       "Extensions can add new source adapters (RSS, news sites) alongside the default X collector.",
        "payload_contract": None,
    },
    "collect.normalize": {
        "name": "collect.normalize",
        "description": "Fired after raw data is fetched, before it is persisted. "
                       "Receives raw source data and must return data conforming to the tweet contract.",
        "payload_contract": "influx://contracts/tweet.schema.json",
    },
    "collect.post": {
        "name": "collect.post",
        "description": "Fired after collection completes and tweets are persisted. "
                       "Extensions can trigger dedup, enrichment, or notifications.",
        "payload_contract": "influx://contracts/tweet.schema.json",
    },
    "influencer.discover": {
        "name": "influencer.discover",
        "description": "Fired when the system evaluates new influencer candidates. "
                       "Extensions can propose accounts from external lists or analytics.",
        "payload_contract": None,
    },
    "influencer.validate": {
        "name": "influencer.validate",
        "description": "Fired to validate an influencer account before adding to a group. "
                       "Extensions can check activity, follower count, or blocklist status.",
        "payload_contract": None,
    },
}

# ---------------------------------------------------------------------------
# Tier 2 -- Classification & Training
# ---------------------------------------------------------------------------
TIER2_HOOKS = {
    "classify.pre": {
        "name": "classify.pre",
        "description": "Fired before classification begins. "
                       "Extensions can filter tweets or inject additional context.",
        "payload_contract": "influx://contracts/tweet.schema.json",
    },
    "classify": {
        "name": "classify",
        "description": "Main classification hook. "
                       "Each registered classifier (keyword, LLM, ML) runs and produces labels.",
        "payload_contract": "influx://contracts/classification.schema.json",
    },
    "classify.fusion": {
        "name": "classify.fusion",
        "description": "Fired after all classifiers have run. "
                       "Extensions can merge / reconcile labels from multiple stages into a final result.",
        "payload_contract": "influx://contracts/classification.schema.json",
    },
    "classify.post": {
        "name": "classify.post",
        "description": "Fired after classification results are persisted. "
                       "Extensions can trigger alerts, dashboards, or downstream processing.",
        "payload_contract": "influx://contracts/classification.schema.json",
    },
    "train.pre": {
        "name": "train.pre",
        "description": "Fired before a model training / fine-tuning run. "
                       "Extensions can prepare datasets or validate training prerequisites.",
        "payload_contract": None,
    },
    "train": {
        "name": "train",
        "description": "Main training hook. "
                       "Extensions can plug in custom training pipelines (few-shot update, ML model retrain).",
        "payload_contract": None,
    },
    "train.post": {
        "name": "train.post",
        "description": "Fired after training completes. "
                       "Extensions can evaluate model quality, register artifacts, or trigger reclassification.",
        "payload_contract": None,
    },
}

# ---------------------------------------------------------------------------
# Tier 3 -- Curation & Publishing
# ---------------------------------------------------------------------------
TIER3_HOOKS = {
    "curate.pre": {
        "name": "curate.pre",
        "description": "Fired before curation begins. "
                       "Extensions can set curation criteria (category filters, time windows, thresholds).",
        "payload_contract": "influx://contracts/classification.schema.json",
    },
    "curate": {
        "name": "curate",
        "description": "Main curation hook. "
                       "Extensions select and rank classified tweets for news item composition.",
        "payload_contract": "influx://contracts/classification.schema.json",
    },
    "compose": {
        "name": "compose",
        "description": "Fired to compose a news item from curated tweets. "
                       "Extensions generate title, body, and format-specific output.",
        "payload_contract": "influx://contracts/news_item.schema.json",
    },
    "schedule": {
        "name": "schedule",
        "description": "Fired to determine publish timing. "
                       "Extensions can implement scheduling strategies (time-of-day, frequency caps).",
        "payload_contract": "influx://contracts/news_item.schema.json",
    },
    "post.pre": {
        "name": "post.pre",
        "description": "Fired before a news item is published. "
                       "Extensions can perform final validation, compliance checks, or approval gates.",
        "payload_contract": "influx://contracts/news_item.schema.json",
    },
    "post": {
        "name": "post",
        "description": "Main publishing hook. "
                       "Extensions deliver the news item to target platforms (X, blog, newsletter).",
        "payload_contract": "influx://contracts/news_item.schema.json",
    },
    "post.post": {
        "name": "post.post",
        "description": "Fired after a news item is published. "
                       "Extensions can track engagement, log results, or trigger follow-up actions.",
        "payload_contract": "influx://contracts/news_item.schema.json",
    },
}
