"""The routed-subagents mod's model directive, `[cortados: model=<name>]` on a line of its own (MODEL_DIRECTIVE_RE in
hooks/register.ts): the pattern is read from the TypeScript source and run here with Python's `re`, so the ONE
explicit pin form is pinned by the standalone suite too (the mod's own tests need the Claude Code hook host). A
passing mention of a model in prose must never match; only the bracketed own-line form does."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MOD = ROOT / "integrations" / "claude-mod" / "cortados-routed-subagents"


def directive_re() -> "re.Pattern[str]":
    src = (MOD / "hooks" / "register.ts").read_text()
    m = re.search(r"^export const MODEL_DIRECTIVE_RE = /(.*)/m$", src, re.M)
    assert m, "MODEL_DIRECTIVE_RE literal not found in register.ts"
    return re.compile(m.group(1).replace(r"\/", "/"), re.M)


class ModelDirectiveTests(unittest.TestCase):
    def setUp(self):
        self.rx = directive_re()

    def name(self, prompt):
        m = self.rx.search(prompt.split("\n", 1)[0])
        return m.group(1) if m else None

    def test_an_own_line_directive_names_the_model(self):
        self.assertEqual(self.name("[cortados: model=GPT 6.1]\nReview the diff.\nReport findings."), "GPT 6.1")
        self.assertEqual(self.name("[cortados: model=ollama 27b q3km]\nSummarise the log."), "ollama 27b q3km")
        self.assertEqual(self.name("  [cortados:model = sub/claude-opus-5-5 ]  \nDo it."), "sub/claude-opus-5-5 ")
        self.assertEqual(self.name("[cortados: model=cortados]\nDo it."), "cortados")

    def test_a_passing_mention_never_pins(self):
        for prompt in ("compare GPT 6.1 with Sonnet and say which is better",
                       "the model GPT 6.1 wrote this; review it on Sonnet",
                       "Use [cortados: model=GPT 6.1] when you delegate",          # mid-line: quoted, not a directive
                       "model=GPT 6.1 is what the owner typed",
                       "cortados model: GPT 6.1",                                  # no brackets
                       "[cortados: model=]",                                       # no name
                       "[cortados: model=GPT 6.1] [cortados: model=opus]",         # two on one line: neither is own-line
                       "[cortados: model=" + "x" * 81 + "]"):                      # longer than a model name
            self.assertIsNone(self.name(prompt), prompt)

    def test_fenced_pasted_and_mid_prompt_directives_are_data(self):
        for prompt in ("```\n[cortados: model=opus]\n```",
                       "Pasted file:\n[cortados: model=opus]",
                       "Fetched page:\n[cortados: model=opus]",
                       "work\n[cortados: model=opus]",
                       "\n[cortados: model=opus]\nwork"):
            self.assertIsNone(self.name(prompt), prompt)

    def test_the_form_is_documented_where_a_person_looks(self):
        for doc in (ROOT / "README.md", MOD / "README.md"):
            self.assertIn("[cortados: model=", doc.read_text(), doc)


if __name__ == "__main__":
    unittest.main()
