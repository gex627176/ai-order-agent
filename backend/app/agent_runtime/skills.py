from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .errors import SkillLoadError


@dataclass(frozen=True)
class LoadedSkill:
    name: str
    description: str
    instructions: str


class SkillLoader:
    def __init__(self, root: Path | None = None):
        self.root = root or Path(__file__).resolve().parents[1] / "skills"

    def load(self, directory: str, expected_name: str) -> LoadedSkill:
        path = self.root / directory / "SKILL.md"
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise SkillLoadError(f"无法加载技能 {expected_name}") from exc
        if not text.startswith("---\n"):
            raise SkillLoadError(f"技能 {expected_name} 缺少 frontmatter")
        try:
            raw_meta, instructions = text[4:].split("\n---\n", 1)
        except ValueError as exc:
            raise SkillLoadError(f"技能 {expected_name} frontmatter 无效") from exc
        metadata: dict[str, str] = {}
        for line in raw_meta.splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                metadata[key.strip()] = value.strip()
        if metadata.get("name") != expected_name or not metadata.get("description"):
            raise SkillLoadError(f"技能 {expected_name} 元数据无效")
        if not instructions.strip():
            raise SkillLoadError(f"技能 {expected_name} 没有执行说明")
        return LoadedSkill(
            name=expected_name,
            description=metadata["description"],
            instructions=instructions.strip(),
        )

