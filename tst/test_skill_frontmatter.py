"""Skills without a frontmatter `description` are omitted from the model's skill listing."""
import re
from pathlib import Path

import pytest

SKILLS = sorted((Path(__file__).resolve().parent.parent / "skills").glob("*/SKILL.md"))


@pytest.mark.parametrize("path", SKILLS, ids=lambda p: p.parent.name)
def test_skill_declares_description(path):
    front = re.match(r"---\n(.*?)\n---\n", path.read_text(), re.S)
    assert front, "missing frontmatter"
    assert re.search(r"^description:\s*\S", front.group(1), re.M)
