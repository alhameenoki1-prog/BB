"""HARVEY_HOME: one install, many isolated Harvey homes."""

import os
import subprocess
import sys
from pathlib import Path

import harvey.brain as brain_mod
import harvey.paths as paths
from harvey.brain import Brain
from harvey.state import StateManager


def test_default_home_is_the_checkout():
    assert paths.HARVEY_HOME == paths.PROJECT_ROOT
    assert paths.DATA_DIR == paths.PROJECT_ROOT / "data"
    assert paths.CONFIG_FILE == paths.PROJECT_ROOT / "harvey.yaml"
    assert paths.HOME_SKILLS_DIR == paths.PROJECT_ROOT / "skills"


def test_env_var_moves_every_instance_path(tmp_path):
    code = (
        "import harvey.paths as p, harvey.state as s, harvey.config as c, harvey.setup as st;"
        "import harvey.integrations.gmail as g, harvey.integrations.microsoft as m, harvey.dashboard as d;"
        "print(p.HARVEY_HOME); print(p.DATA_DIR); print(s.DB_PATH); print(g.TOKEN_FILE);"
        "print(m.TOKEN_FILE); print(st.CONFIG_FILE); print(st.ENV_FILE); print(d.DB_PATH);"
        "print(p.PROMPTS_DIR); print(p.SKILLS_DIR); print(c._find_config_file.__module__)"
    )
    env = {**os.environ, "HARVEY_HOME": str(tmp_path / "home")}
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True,
                         check=True, cwd=str(paths.PROJECT_ROOT)).stdout.splitlines()
    home = (tmp_path / "home").resolve()
    assert Path(out[0]) == home
    assert Path(out[1]) == home / "data"
    assert Path(out[2]) == home / "data" / "harvey.db"
    assert Path(out[3]) == home / "data" / "gmail_token.json"
    assert Path(out[4]) == home / "data" / "m365_token.json"
    assert Path(out[5]) == home / "harvey.yaml" and Path(out[6]) == home / ".env"
    assert Path(out[7]) == home / "data" / "harvey.db"
    # Code and shipped knowledge stay with the checkout.
    assert Path(out[8]) == paths.PROJECT_ROOT / "prompts"
    assert Path(out[9]) == paths.PROJECT_ROOT / "skills"


def test_home_config_wins_over_cwd_and_checkout(tmp_path, monkeypatch):
    import harvey.config as config_mod
    home_cfg = tmp_path / "home" / "harvey.yaml"
    home_cfg.parent.mkdir()
    home_cfg.write_text("persona: {}")
    monkeypatch.setattr(config_mod, "CONFIG_FILE", home_cfg)
    monkeypatch.setattr(config_mod, "LOCAL_CONFIG_FILE", tmp_path / "home" / "harvey.local.yaml")
    assert config_mod._find_config_file() == str(home_cfg)


def test_home_skill_overrides_shipped_skill(tmp_path, monkeypatch):
    home_skills = tmp_path / "skills"
    home_skills.mkdir()
    (home_skills / "product_knowledge.md").write_text("# Per-home knowledge")
    monkeypatch.setattr(brain_mod, "HOME_SKILLS_DIR", home_skills)
    brain = Brain(StateManager(str(tmp_path / "x.db")), client=object())
    assert brain.load_skill("product_knowledge") == "# Per-home knowledge"
    # Skills the home doesn't override still come from the checkout.
    assert "email" in brain.load_skill("email_frameworks").lower()


def test_needs_setup_accepts_credentials_from_the_environment(tmp_path, monkeypatch):
    import harvey.main as main_mod
    monkeypatch.setattr("harvey.paths.ENV_FILE", tmp_path / "missing.env")
    cfg = tmp_path / "harvey.yaml"
    cfg.write_text("persona:\n  company: Real Co\n")
    monkeypatch.setattr("harvey.config._find_config_file", lambda: str(cfg))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    for k in ("GMAIL_CLIENT_ID", "SMTP_HOST", "M365_CLIENT_ID", "INSTANTLY_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    assert main_mod._needs_setup() is True       # no .env, nothing in the environment
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    assert main_mod._needs_setup() is False      # a supervisor passed credentials in


def test_decision_respects_auto_prospect(tmp_path):
    import asyncio
    from harvey.config import HarveyConfig
    from harvey.main import decide_next_action
    base = {
        "persona": {"name": "H", "company": "A", "role": "B", "email": "e", "linkedin": "", "tone": "t"},
        "product": {"name": "P", "description": "D", "pricing": "$1", "key_benefits": [],
                    "objection_responses": {}},
        "icp": {"industries": [], "company_size": "", "titles": [], "geography": []},
    }
    summary = {"prospects": {"new": 0}, "draft_campaigns": 0, "open_conversations": 0}
    on = HarveyConfig(**base)
    off = HarveyConfig(**{**base, "icp": {**base["icp"], "auto_prospect": False}})
    assert asyncio.run(decide_next_action(None, None, on, summary=summary)) == "prospect"
    assert asyncio.run(decide_next_action(None, None, off, summary=summary)) == "idle"
    summary["prospects"]["new"] = 3
    assert asyncio.run(decide_next_action(None, None, off, summary=summary)) == "write_campaign"
