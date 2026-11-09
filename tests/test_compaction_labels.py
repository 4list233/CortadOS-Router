import unittest

from cortados_router.compaction.events import Message, Session, ToolEvent
from cortados_router.compaction.labels import command_has_side_effect, has_side_effect, label_session, target

LONG_LINE = "def resolve(request, answers, degraded=False, engine='s1'):  # the band decision"


def ev(i, mi, name, inp, result="", **kw):
    return ToolEvent(session="s", index=i, message_index=mi, name=name, input=inp, result_text=result,
                     result_chars=len(result), is_error=False, interrupted=kw.get("interrupted", False))


def msg(i, role, text):
    return Message(session="s", index=i, role=role, text=text)


def session(events, texts):
    return Session(session="s", source="test", messages=tuple(msg(i, r, t) for i, (r, t) in enumerate(texts)),
                   events=tuple(events))


FILLER = [("user", "go on"), ("assistant", "ok"), ("user", "more"), ("assistant", "done")]


class LabelTests(unittest.TestCase):
    def rows(self, events, texts, horizon=4):
        return {r["event_index"]: r for r in label_session(session(events, texts), horizon=horizon)}

    def test_reread_same_file_is_needed(self):
        r = self.rows([ev(0, 1, "Read", {"file_path": "/a/core.py"}, "x = 1"),
                       ev(1, 3, "Read", {"file_path": "/a/core.py"}, "x = 1")],
                      [("user", "fix"), ("assistant", "reading"), ("user", "again"), ("assistant", "rereading")] + FILLER)
        self.assertTrue(r[0]["needed_result"])
        self.assertEqual(r[0]["evidence"][0]["rule"], "rerun")
        self.assertEqual(r[0]["evidence"][0]["index"], 1)

    def test_rerun_normalizes_whitespace(self):
        r = self.rows([ev(0, 1, "Bash", {"command": "ls   -la"}, "a"), ev(1, 2, "Bash", {"command": "ls -la"}, "a")],
                      [("user", "u"), ("assistant", "a"), ("assistant", "b")] + FILLER)
        self.assertEqual(r[0]["evidence"][0]["rule"], "rerun")

    def test_quote_of_long_line(self):
        r = self.rows([ev(0, 1, "Read", {"file_path": "/a/core.py"}, "short\n" + LONG_LINE + "\n")],
                      [("user", "u"), ("assistant", "a"), ("assistant", "I will change `" + LONG_LINE + "` now")] + FILLER)
        self.assertTrue(r[0]["needed_result"])
        self.assertEqual(r[0]["evidence"][0]["rule"], "quote")

    def test_identifier_first_seen_in_result(self):
        r = self.rows([ev(0, 1, "Bash", {"command": "git log -1"}, "commit 9fe1b29c0ffee\nAuthor: x")],
                      [("user", "u"), ("assistant", "a"), ("assistant", "reverting 9fe1b29c0ffee")] + FILLER)
        self.assertEqual(r[0]["evidence"][0]["rule"], "identifier")

    def test_identifier_already_known_does_not_count(self):
        r = self.rows([ev(0, 1, "Bash", {"command": "git log -1"}, "commit 9fe1b29c0ffee")],
                      [("user", "look at 9fe1b29c0ffee"), ("assistant", "a"), ("assistant", "9fe1b29c0ffee again")] + FILLER)
        self.assertFalse(r[0]["needed_result"])

    def test_never_referenced_is_false(self):
        r = self.rows([ev(0, 1, "Grep", {"pattern": "foo"}, "nothing useful here")],
                      [("user", "u"), ("assistant", "a"), ("assistant", "unrelated")] + FILLER)
        self.assertEqual(r[0]["status"], "labelled")
        self.assertFalse(r[0]["needed_result"])
        self.assertFalse(r[0]["needed_call"])
        self.assertEqual(r[0]["evidence"], [])

    def test_side_effect_keeps_call_not_result(self):
        r = self.rows([ev(0, 1, "Edit", {"file_path": "/a/x.py", "old_string": "a", "new_string": "b"}, "ok"),
                       ev(1, 1, "Bash", {"command": "git commit -m wip"}, "[main abc] wip")],
                      [("user", "u"), ("assistant", "a"), ("assistant", "b")] + FILLER)
        for i in (0, 1):
            self.assertTrue(r[i]["needed_call"])
            self.assertFalse(r[i]["needed_result"])
            self.assertEqual(r[i]["evidence"][0]["rule"], "side_effect")

    def test_later_write_is_not_a_reread(self):
        r = self.rows([ev(0, 1, "Read", {"file_path": "/a/x.py"}, "body"),
                       ev(1, 2, "Write", {"file_path": "/a/x.py", "content": "new"}, "ok")],
                      [("user", "u"), ("assistant", "a"), ("assistant", "b")] + FILLER)
        self.assertFalse(r[0]["needed_result"])
        self.assertTrue(r[0]["needed_call"])          # the later write names the same path
        self.assertEqual(r[0]["evidence"][0]["rule"], "target")

    def test_target_mention_in_message(self):
        r = self.rows([ev(0, 1, "Read", {"file_path": "/a/zz.py"}, "body")],
                      [("user", "u"), ("assistant", "a"), ("assistant", "as /a/zz.py showed")] + FILLER)
        self.assertFalse(r[0]["needed_result"])
        self.assertTrue(r[0]["needed_call"])

    def test_tail_events_are_unknown(self):
        r = self.rows([ev(0, 1, "Read", {"file_path": "/a"}, "x"), ev(1, 6, "Read", {"file_path": "/b"}, "y")],
                      [("user", "u"), ("assistant", "a")] + FILLER + [("assistant", "z")], horizon=4)
        self.assertEqual(r[0]["status"], "labelled")
        self.assertEqual(r[1]["status"], "unknown")
        self.assertIsNone(r[1]["needed_result"])
        self.assertEqual(r[1]["horizon"]["min_future_messages"], 4)

    def test_interrupted_has_no_needed_result(self):
        r = self.rows([ev(0, 1, "Bash", {"command": "make"}, "", interrupted=True),
                       ev(1, 2, "Bash", {"command": "make"}, "built")],
                      [("user", "u"), ("assistant", "a"), ("assistant", "b")] + FILLER)
        self.assertFalse(r[0]["needed_result"])

    def test_quote_with_quotes_and_backslashes_in_edit_input(self):
        line = 'pattern = re.compile(r"\\d+\\s*\"quoted\"")  # a regex line with escapes in it'
        r = self.rows([ev(0, 1, "Read", {"file_path": "/a/re.py"}, "x\n" + line + "\n"),
                       ev(1, 2, "Edit", {"file_path": "/a/re.py", "old_string": line, "new_string": "y"}, "ok")],
                      [("user", "u"), ("assistant", "a"), ("assistant", "b")] + FILLER)
        self.assertTrue(r[0]["needed_result"])
        self.assertEqual(r[0]["evidence"][0]["rule"], "quote")
        self.assertEqual(r[0]["evidence"][0]["index"], 1)

    def test_identifier_in_nested_input_list(self):
        r = self.rows([ev(0, 1, "Bash", {"command": "git log -1"}, 'commit "9fe1b29c0ffee"'),
                       ev(1, 2, "Tool", {"args": [{"ref": "9fe1b29c0ffee\\x"}]}, "ok")],
                      [("user", "u"), ("assistant", "a"), ("assistant", "b")] + FILLER)
        self.assertEqual(r[0]["evidence"][0]["rule"], "identifier")

    def test_git_global_options_before_mutating_subcommand(self):
        for c in ("git -C /repo commit -m fix", "git -C /repo push origin main",
                  "git -c user.name=x --no-pager --git-dir=/r/.git --work-tree /r commit -am y",
                  "cd /r && git -C x add .", "echo hi > out.txt", "FOO=1 sudo rm -rf build", "sed -i '' s/a/b/ f"):
            self.assertTrue(command_has_side_effect(c), c)
        r = self.rows([ev(0, 1, "Bash", {"command": "git -C /repo commit -m fix"}, "[main 1a2b3c4] fix")],
                      [("user", "u"), ("assistant", "a"), ("assistant", "b")] + FILLER)
        self.assertTrue(r[0]["needed_call"])
        self.assertEqual(r[0]["evidence"][0]["rule"], "side_effect")

    def test_read_only_commands_are_not_side_effects(self):
        for c in ("git -C /repo status", "git -C /repo log --oneline -5", "git --no-pager diff",
                  "git --git-dir=/r/.git show HEAD", "ls > /dev/null 2>&1", "grep commit file", "cat a | grep rm",
                  "pip list", "curl -s https://x", "ls -la", "git status", "git log", "grep foo f", "cat f", "head f"):
            self.assertFalse(command_has_side_effect(c), c)
        # `git -c key=value` sets any config (core.pager, diff.external, an alias): refused, even before `show`.
        self.assertTrue(command_has_side_effect("git -c core.pager=cat show HEAD"))

    def test_io_number_is_only_the_digits_adjacent_to_the_redirect(self):
        # A separate numeric argument is an argument (duration, niceness, uid, path), not a file descriptor; the
        # old classifier popped it off the segment and lost the real program.
        for c in ("timeout 5 </dev/null rm x", "nice -n 5 </dev/null rm x", "sudo -u 1000 </dev/null rm x",
                  "git -C 1 </dev/null commit -m x", "timeout 5 >/dev/null rm x"):
            with self.subTest(command=c):
                self.assertTrue(command_has_side_effect(c))
        for c in ("ls 2>&1", "ls 2 >&1", "0</dev/null cat", "timeout 5 </dev/null cat f", "cat 1>&2 f"):
            with self.subTest(command=c):
                self.assertFalse(command_has_side_effect(c))

    def test_in_place_editing_flags(self):
        for c in ("perl -pi -e s/a/b/ f", "perl -i.bak -pe s/a/b/ f", "sed -Ei s/a/b/ f", "sed -ni p f",
                  "sed --in-place s/a/b/ f", "sed --in-place=.bak s/a/b/ f", "sed -I '' s/a/b/ f", "sed -ie s/a/b/ f"):
            with self.subTest(command=c):
                self.assertTrue(command_has_side_effect(c))

    def test_sed_scripts_that_write_or_execute(self):
        for c in ("sed 's/a/b/w out' f", "sed -n '/x/w out.txt' f", "sed 'e touch x' f", "sed 's/a/b/e' f",
                  "sed -f script f", "sed -e 's/a/b/' -e 'w out' f", "sed --expression='w out' f", "sed 'W out' f",
                  "sed -n '1,3{p;w out\n}' f", "sed 's/a/b/gw out' f", "sed", "sed '/x/' f"):
            with self.subTest(command=c):
                self.assertTrue(command_has_side_effect(c))
        for c in ("sed 's/a/b/' f", "sed -n p f", "sed -n '1,10p' f", "sed -E 's/(a)/\\1/g' f",
                  "sed -e 's/a/b/' -e '/x/d' f", "sed '$d' f", "sed -n '/re/,/re2/p' f", "sed 's/hello/world/' f",
                  "sed 's|a/b|c|' f", "sed '/x/!d' f", "sed -n '2{p;q}' f", "sed 'N;P;D' f", "sed ':a;N;$!ba;s/\\n/ /g' f",
                  "sed 's/[/]/x/' f", "sed -n '$=' f", "sed 'y/abc/xyz/' f", "sed --quiet -e p f", "cat f | sed 's/a/b/'",
                  "sed p"):
            with self.subTest(command=c):
                self.assertFalse(command_has_side_effect(c))

    def test_wrappers_and_executors_are_side_effects(self):
        for c in ("xargs rm", "echo x | xargs rm", "find . -delete", "find . -exec rm {} \\;", "find . -execdir rm {} +",
                  "find . -ok rm {} \\;", "find . -fprint out", "find . -fprintf out %p", "find . -fls out",
                  "stdbuf -o0 rm x", "setsid rm x", "gtimeout 5 rm x", "doas rm x", "flock f rm x", "watch rm x",
                  "caffeinate rm x", "ionice rm x", "busybox rm x", "trap 'rm x' EXIT", "ksh -c 'rm x'",
                  "fish -c 'rm x'", "rg --pre cat x", "fd -x rm", "fd --exec rm"):
            with self.subTest(command=c):
                self.assertTrue(command_has_side_effect(c))
        for c in ("find . -name x", "find . -type f -print", "find . -newer a -not -name b", "rg foo", "fd pat"):
            with self.subTest(command=c):
                self.assertFalse(command_has_side_effect(c))

    def test_unknown_programs_are_side_effects(self):
        for c in ("git branch -D x", "git worktree remove x", "git config user.name x", "git -c alias.st=commit st",
                  "curl -XPOST https://x", "curl -X POST https://x", "curl -d a=b https://x", "curl -F f=@x https://x",
                  "curl -T f https://x", "curl -o f https://x", "curl -sSLo f https://x", "curl -O https://x",
                  "curl -K cfg https://x", "pip install x", "pip3 install x", "npm install x", "npm i x",
                  "python3 -c 'print(1)'", "node -e 'x'", "awk 'BEGIN{system(\"rm x\")}'", "ssh h rm x", "scp a h:b",
                  "rsync a b", "wget https://x", "kill 1", "tar xf a.tgz", "tar tf a.tgz", "unzip a.zip",
                  "gh pr merge 1", "gh api -X POST repos/x", "gh api -f a=b x", "gh pr view 1 --web", "docker rm c",
                  "docker ps", "launchctl load x", "unknownprog", "./script.sh", "make", "python3 -m unittest",
                  "export FOO=1", "brew install x", "sort -o out f", "sort -no out f", "uniq in out", "date -s now",
                  "date 1201", "file -C -m magic", "tree -o out.txt", "hostname newname", "hostname -F f",
                  "arch -x86_64 rm x", "git tag v1", "git tag -d v1", "git branch foo", "git stash", "git stash pop",
                  "git worktree add x", "git remote add o u", "git reflog expire --all", "git -p log",
                  "git --exec-path=/x status", "git log --output=f", "git diff --ext-diff", "git grep -O rm",
                  "git config user.name", "PATH=/tmp ls", "HOME=/tmp git diff", "env PAGER=x git log", "LD_PRELOAD=x ls"):
            with self.subTest(command=c):
                self.assertTrue(command_has_side_effect(c))

    def test_allowlisted_argument_shapes_stay_read_only(self):
        for c in ("git branch", "git branch -a", "git branch -vv", "git branch --list 'x'", "git branch --show-current",
                  "git tag", "git tag -l", "git tag -n5", "git stash list", "git stash show", "git worktree list",
                  "git remote -v", "git remote show origin", "git config --get user.name", "git config --list",
                  "git config -l --show-origin", "git reflog", "git reflog show", "git rev-parse HEAD",
                  "git --version", "git describe --tags", "git grep -n rm",
                  "curl -X GET https://x", "curl -XGET -I https://x", "curl --request HEAD https://x",
                  "curl -sSL -H 'A: b' https://x", "curl --max-time 5 -u a:b https://x",
                  "python3 --version", "node -v", "npm ls", "pip show x", "brew list",
                  "sort f", "sort -n f", "uniq f", "uniq -c", "date", "date +%s", "date -u",
                  "gh issue view 60", "gh pr list", "gh api repos/x/issues", "gh api -X GET repos/x",
                  "command -v git", "cd /r && ls", "jq . f", "wc -l f", "tail -n 5 f", "FOO=1 ls",
                  "file x.bin", "tree -L 2", "hostname", "hostname -f", "arch"):
            with self.subTest(command=c):
                self.assertFalse(command_has_side_effect(c))

    def test_quoted_or_escaped_operator_words_are_unsupported(self):
        for c in ("git -C '<' commit -m x", "git -C \\< commit -m x", 'git -C "<" commit -m x', "echo ';' ; rm x",
                  "echo '|'", "echo \\;"):
            with self.subTest(command=c):
                self.assertTrue(command_has_side_effect(c))
        self.assertFalse(command_has_side_effect("echo 'a;b'"))     # quoted text that merely contains an operator

    def test_equals_prefix_and_case_folded_names(self):
        for c in ("=rm x", "RM x", "Rm x", "GIT -C r commit -m x", "=ls"):
            with self.subTest(command=c):
                self.assertTrue(command_has_side_effect(c))
        self.assertFalse(command_has_side_effect("LS -la"))         # a case-insensitive filesystem runs ls

    def test_non_string_command_is_a_side_effect(self):
        for inp in ({"command": ["bash", "-lc", "git push"]}, {"command": {"argv": ["ls"]}}, {"cmd": 42},
                    {"command": ["ls"]}):
            with self.subTest(input=inp):
                self.assertTrue(has_side_effect(ev(0, 0, "Bash", inp)))
        self.assertFalse(has_side_effect(ev(0, 0, "Bash", {"command": "ls"})))
        self.assertFalse(has_side_effect(ev(0, 0, "Bash", {"command": None})))   # JSON null: no command at all

    def test_multiline_commands_keep_boundaries(self):
        # An unquoted newline ends a command: the second line must not read as arguments of the first.
        for c in ("git status\ngit -C /repo commit -m fix", "ls\nrm -rf build", "cd /r &&\ngit -C x add .",
                  "ls # a comment\nrm x"):
            self.assertTrue(command_has_side_effect(c), c)
        for c in ("git status\ngit -C /repo log -1", "ls\ncat a.txt\ngrep rm b.txt", "echo 'rm x\nrm y'",
                  "echo \"git\ncommit\"", "ls;\ncat x"):
            self.assertFalse(command_has_side_effect(c), c)

    def test_wrapper_options_and_assignments_are_unwrapped(self):
        for c in ("env FOO=1 git -C /repo commit -m fix", "sudo -u user rm -rf x", "sudo -E env -i X=1 touch f",
                  "nohup env -u HOME git push", "timeout 30 rm -r x", "nice -n 10 mkdir d", "sudo -- rm x",
                  "FOO=1 env BAR=2 sudo -g wheel -u root git -C /r commit -m x"):
            self.assertTrue(command_has_side_effect(c), c)
        for c in ("env FOO=1 git -C /repo status", "sudo -u user ls -la", "env", "sudo -u", "timeout 30 cat f",
                  "env -i X=1 grep rm f"):
            self.assertFalse(command_has_side_effect(c), c)

    def test_dup_redirect_writes_file_but_not_descriptor(self):
        for c in ("echo hello >&out.txt", "echo hello >& out.txt", "ls 2>&1 >&log.txt"):
            self.assertTrue(command_has_side_effect(c), c)
        for c in ("ls 2>&1", "echo warn >&2", "ls 1>&2", "ls >&-", "ls >&/dev/null", "ls 2>&1 | grep x"):
            self.assertFalse(command_has_side_effect(c), c)

    def test_parse_error_keeps_completed_prefix(self):
        # A shell runs the first line before hitting the unterminated quote: the commit must still count.
        for c in ('git -C /repo commit -m fix\necho "unterminated', "touch f\necho 'open", "git -C r add .\necho a\\"):
            self.assertTrue(command_has_side_effect(c), c)
        for c in ('git -C /repo status\necho "unterminated', 'echo "unterminated'):
            # Incomplete syntax is now conservative, not confidently read-only.
            self.assertTrue(command_has_side_effect(c), c)
        self.assertTrue(command_has_side_effect('echo "x && rm -rf y'))

    def test_shell_comments_preserve_command_boundary(self):
        self.assertTrue(command_has_side_effect("echo ok # user's note\ntouch marker"))
        for c in ("true # noop; touch marker", "true # user's note", "echo '# touch marker'",
                  'echo "# touch marker"', "echo x#not-comment", r"echo \#literal"):
            with self.subTest(command=c):
                self.assertFalse(command_has_side_effect(c))

    def test_env_split_string_is_conservative(self):
        for option in ("-S 'touch marker'", "--split-string 'touch marker'",
                       "--split-string=touch", "-Stouch", "-iS 'touch marker'", "-S 'true'"):
            with self.subTest(option=option):
                self.assertTrue(command_has_side_effect("env " + option))
                self.assertTrue(command_has_side_effect("sudo env " + option))

    def test_unknown_wrapper_options_are_conservative(self):
        for c in ("env --argv0 name touch marker", "sudo --user root touch marker",
                  "time -f format touch marker", "env --unknown true"):
            with self.subTest(command=c):
                self.assertTrue(command_has_side_effect(c))
        # A normal program's -S option is not env's split-string syntax.
        self.assertFalse(command_has_side_effect("git log -S needle"))

    def test_redirect_operands_do_not_hide_executable(self):
        for c in ("< /dev/null touch marker", "0</dev/null touch marker", ">/dev/null touch marker",
                  "2>&1 touch marker", "env < /dev/null touch marker", "< /dev/null git add .",
                  "true\n< /dev/null touch marker"):
            with self.subTest(command=c):
                self.assertTrue(command_has_side_effect(c))
        for c in ("< /dev/null cat", "0</dev/null cat", "cat < input", "env < input git status",
                  "true > /dev/null\ncat < input"):
            with self.subTest(command=c):
                self.assertFalse(command_has_side_effect(c))

    def test_unmodelled_shell_syntax_is_conservative(self):
        for c in ("echo $(touch marker)", "echo $COMMAND", "echo <(touch marker)",
                  "cat <<EOF\ntext\nEOF", "cat <<< text", "(true)", "{ true; }",
                  "if true; then touch marker; fi", "for x in a; do touch marker; done",
                  "eval 'touch marker'", "sh -c 'touch marker'", "source script", ". script",
                  "coproc touch marker", "builtin eval touch marker", "echo `touch marker`",
                  "echo *", "true >>> file", "cat <", 'echo "unfinished'):
            with self.subTest(command=c):
                self.assertTrue(command_has_side_effect(c))
        for c in ("echo '$COMMAND'", "echo '$(literal)'", "echo '{literal}'"):
            with self.subTest(command=c):
                self.assertFalse(command_has_side_effect(c))

    def test_target_kinds(self):
        self.assertEqual(target(ev(0, 0, "Grep", {"pattern": " a  b "})), ("query", "grep:a b"))
        self.assertIsNone(target(ev(0, 0, "Other", {"x": 1})))


if __name__ == "__main__":
    unittest.main()
