import json
import re
from pathlib import Path

PLATFORMS = Path(__file__).resolve().parent / 'verification-platforms.json'
OWNER = r'[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?'
REPOSITORY = OWNER + r'/[A-Za-z0-9_.-]+'


def verification_policy():
    if not PLATFORMS.exists():
        return {'linux_hosts': [], 'repositories': {}, 'macos_hosts': [], 'macos_repositories': {}, 'auxiliary_linux_repositories': {}}
    policy = json.loads(PLATFORMS.read_text())
    if (set(policy) - {'coldbox_host', 'owner'} != {'linux_hosts', 'repositories', 'macos_hosts', 'macos_repositories', 'auxiliary_linux_repositories'}
            or not isinstance(policy.get('coldbox_host', 'unset'), str)
            or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9.-]*', policy.get('coldbox_host', 'unset'))
            or not isinstance(policy.get('owner', 'unset'), str)
            or not re.fullmatch(OWNER, policy.get('owner', 'unset'))):
        raise ValueError('INCOMPLETE: invalid native verification policy')
    seen = set()
    for hosts_key, repos_key in [('linux_hosts', 'repositories'), ('macos_hosts', 'macos_repositories'), ('linux_hosts', 'auxiliary_linux_repositories')]:
        hosts, repos = policy[hosts_key], policy[repos_key]
        if (not isinstance(hosts, list) or not hosts
                or any(not isinstance(h, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9.-]*', h) for h in hosts)
                or not isinstance(repos, dict)
                or any(not re.fullmatch(REPOSITORY, name) or not isinstance(reason, str) or not reason.strip()
                       for name, reason in repos.items())):
            raise ValueError('INCOMPLETE: invalid native verification policy')
        names = {name.casefold() for name in repos}
        if seen & names:
            raise ValueError('INCOMPLETE: repository has conflicting native platforms')
        seen.update(names)
    return policy


def macos_repository(repo):
    return repo.casefold() in {name.casefold() for name in verification_policy()['macos_repositories']}


def linux_repository(repo):
    return repo.casefold() in {name.casefold() for name in verification_policy()['repositories']}


def linux_runner_repository(repo):
    owner = verification_policy().get('owner')
    return (owner is not None and re.fullmatch(REPOSITORY, repo) is not None
            and repo.split('/')[0].casefold() == owner.casefold() and not macos_repository(repo))
