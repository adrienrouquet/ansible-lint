"""All internal ansible-lint rules."""
import copy
import glob
import importlib.util
import logging
import os
import re
from collections import defaultdict
from importlib.abc import Loader
from time import sleep
from typing import List, Optional

import ansiblelint.utils
from ansiblelint._internal.rules import AnsibleParserErrorRule, LoadingFailureRule
from ansiblelint.errors import BaseRule, MatchError, RuntimeErrorRule
from ansiblelint.skip_utils import append_skipped_rules, get_rule_skips_from_line

_logger = logging.getLogger(__name__)


class AnsibleLintRule(BaseRule):

    def __repr__(self) -> str:
        """Return a AnsibleLintRule instance representation."""
        return self.id + ": " + self.shortdesc

    @staticmethod
    def unjinja(text):
        text = re.sub(r"{{.+?}}", "JINJA_EXPRESSION", text)
        text = re.sub(r"{%.+?%}", "JINJA_STATEMENT", text)
        text = re.sub(r"{#.+?#}", "JINJA_COMMENT", text)
        return text

    def create_matcherror(
            self,
            message: str = None,
            linenumber: int = 0,
            details: str = "",
            filename: str = None,
            tag: str = "") -> MatchError:
        match = MatchError(
            message=message,
            linenumber=linenumber,
            details=details,
            filename=filename,
            rule=copy.copy(self)
            )
        if tag:
            match.tag = tag
        return match

    def matchlines(self, file, text) -> List[MatchError]:
        matches: List[MatchError] = []
        if not self.match:
            return matches
        # arrays are 0-based, line numbers are 1-based
        # so use prev_line_no as the counter
        for (prev_line_no, line) in enumerate(text.split("\n")):
            if line.lstrip().startswith('#'):
                continue

            rule_id_list = get_rule_skips_from_line(line)
            if self.id in rule_id_list:
                continue

            result = self.match(line)
            if not result:
                continue
            message = None
            if isinstance(result, str):
                message = result
            m = self.create_matcherror(
                message=message,
                linenumber=prev_line_no + 1,
                details=line,
                filename=file['path'])
            matches.append(m)
        return matches

    # TODO(ssbarnea): Reduce mccabe complexity
    # https://github.com/ansible-community/ansible-lint/issues/744
    def matchtasks(self, file: str, text: str) -> List[MatchError]:  # noqa: C901
        matches: List[MatchError] = []
        if not self.matchtask:
            return matches

        if file['type'] == 'meta':
            return matches

        yaml = ansiblelint.utils.parse_yaml_linenumbers(text, file['path'])
        if not yaml:
            return matches

        yaml = append_skipped_rules(yaml, text, file['type'])

        try:
            tasks = ansiblelint.utils.get_normalized_tasks(yaml, file)
        except MatchError as e:
            return [e]

        for task in tasks:
            if self.id in task.get('skipped_rules', ()):
                continue

            if 'action' not in task:
                continue
            result = self.matchtask(file, task)
            if not result:
                continue

            message = None
            if isinstance(result, str):
                message = result
            task_msg = "Task/Handler: " + ansiblelint.utils.task_to_str(task)
            m = self.create_matcherror(
                message=message,
                linenumber=task[ansiblelint.utils.LINE_NUMBER_KEY],
                details=task_msg,
                filename=file['path'])
            matches.append(m)
        return matches

    @staticmethod
    def _matchplay_linenumber(play, optional_linenumber):
        try:
            linenumber, = optional_linenumber
        except ValueError:
            linenumber = play[ansiblelint.utils.LINE_NUMBER_KEY]
        return linenumber

    def matchyaml(self, file: dict, text: str) -> List[MatchError]:
        matches: List[MatchError] = []
        if not self.matchplay:
            return matches

        yaml = ansiblelint.utils.parse_yaml_linenumbers(text, file['path'])
        # yaml returned can be an AnsibleUnicode (a string) when the yaml
        # file contains a single string. YAML spec allows this but we consider
        # this an fatal error.
        if isinstance(yaml, str):
            return [MatchError(
                filename=file['path'],
                rule=LoadingFailureRule()
            )]
        if not yaml:
            return matches

        if isinstance(yaml, dict):
            yaml = [yaml]

        yaml = ansiblelint.skip_utils.append_skipped_rules(yaml, text, file['type'])

        for play in yaml:

            # Bug #849
            if play is None:
                continue

            if self.id in play.get('skipped_rules', ()):
                continue

            result = self.matchplay(file, play)
            if not result:
                continue

            if isinstance(result, tuple):
                result = [result]

            if not isinstance(result, list):
                raise TypeError("{} is not a list".format(result))

            for section, message, *optional_linenumber in result:
                linenumber = self._matchplay_linenumber(play, optional_linenumber)
                matches.append(self.create_matcherror(
                    message=message,
                    linenumber=linenumber,
                    details=str(section),
                    filename=file['path']
                    ))
        return matches


def load_plugins(directory: str) -> List[AnsibleLintRule]:
    """Return a list of rule classes."""
    result = []

    for pluginfile in glob.glob(os.path.join(directory, '[A-Za-z]*.py')):

        pluginname = os.path.basename(pluginfile.replace('.py', ''))
        spec = importlib.util.spec_from_file_location(pluginname, pluginfile)
        # https://github.com/python/typeshed/issues/2793
        if spec and isinstance(spec.loader, Loader):
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            obj = getattr(module, pluginname)()
            result.append(obj)
    return result


class RulesCollection(object):

    def __init__(self, rulesdirs=None) -> None:
        """Initialize a RulesCollection instance."""
        if rulesdirs is None:
            rulesdirs = []
        self.rulesdirs = ansiblelint.file_utils.expand_paths_vars(rulesdirs)
        self.rules: List[BaseRule] = []
        # internal rules included in order to expose them for docs as they are
        # not directly loaded by our rule loader.
        self.rules.extend(
            [RuntimeErrorRule(), AnsibleParserErrorRule(), LoadingFailureRule()])
        for rulesdir in self.rulesdirs:
            _logger.debug("Loading rules from %s", rulesdir)
            self.extend(load_plugins(rulesdir))
        self.rules = sorted(self.rules)

    def register(self, obj: AnsibleLintRule) -> None:
        self.rules.append(obj)

    def __iter__(self):
        """Return the iterator over the rules in the RulesCollection."""
        return iter(self.rules)

    def __len__(self):
        """Return the length of the RulesCollection data."""
        return len(self.rules)

    def extend(self, more: List[AnsibleLintRule]) -> None:
        self.rules.extend(more)

    def run(self, playbookfile, tags=set(), skip_list=frozenset()) -> List:
        text = ""
        matches: List = list()
        error: Optional[IOError] = None

        for i in range(3):
            try:
                with open(playbookfile['path'], mode='r', encoding='utf-8') as f:
                    text = f.read()
                break
            except IOError as e:
                _logger.warning(
                    "Couldn't open %s - %s [try:%s]",
                    playbookfile['path'],
                    e.strerror,
                    i)
                error = e
                sleep(1)
                continue
        else:
            return [MatchError(
                message=str(error),
                filename=playbookfile['path'],
                rule=LoadingFailureRule())]

        for rule in self.rules:
            if not tags or not set(rule.tags).union([rule.id]).isdisjoint(tags):
                rule_definition = set(rule.tags)
                rule_definition.add(rule.id)
                if set(rule_definition).isdisjoint(skip_list):
                    matches.extend(rule.matchlines(playbookfile, text))
                    matches.extend(rule.matchtasks(playbookfile, text))
                    matches.extend(rule.matchyaml(playbookfile, text))

        # some rules can produce matches with tags that are inside our
        # skip_list, so we need to cleanse the matches
        matches = [m for m in matches if m.tag not in skip_list]

        return matches

    def __repr__(self) -> str:
        """Return a RulesCollection instance representation."""
        return "\n".join([rule.verbose()
                          for rule in sorted(self.rules, key=lambda x: x.id)])

    def listtags(self) -> str:
        tag_desc = {
            "behaviour": "Indicates a bad practice or behavior",
            "bug": "Likely wrong usage pattern",
            "command-shell": "Specific to use of command and shell modules",
            "core": "Related to internal implementation of the linter",
            "deprecations": "Indicate use of features that are removed from Ansible",
            "experimental": "Newly introduced rules, by default triggering only warnings",
            "formatting": "Related to code-style",
            "idempotency":
                "Possible indication that consequent runs would produce different results",
            "idiom": "Anti-pattern detected, likely to cause undesired behavior",
            "metadata": "Invalid metadata, likely related to galaxy, collections or roles",
            "module": "Incorrect module usage",
            "readability": "Reduce code readability",
            "repeatability": "Action that may produce different result between runs",
            "resources": "Unoptimal feature use",
            "safety": "Increase security risk",
            "task": "Rules specific to tasks",
            "unpredictability": "This will produce unexpected behavior when run",
        }

        tags = defaultdict(list)
        for rule in self.rules:
            for tag in rule.tags:
                tags[tag].append(rule.id)
        result = "# List of tags and how they are used\n"
        for tag in sorted(tags):
            desc = tag_desc.get(tag, None)
            if desc:
                result += f"{tag}:  # {desc}\n"
            else:
                result += f"{tag}:\n"
            result += f"  rules: [{', '.join(tags[tag])}]\n"
        return result
