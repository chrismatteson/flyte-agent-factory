"""A fake of the study's `inputs`: the brief and acceptance come straight from the snapshot."""


def load_launch_briefs() -> dict:
    return {}


def build_prompt_job(row: dict, snapshot: dict, briefs: dict) -> tuple[dict, str, str]:
    return {"brief": snapshot["brief"], "acceptance": snapshot["acceptance"]}, "exact", "fake"
