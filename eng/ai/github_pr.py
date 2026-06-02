import os
import tempfile
from typing import Dict

def create_pr_stub(repo_path: str, branch_name: str, changes: Dict[str, str]) -> str:
    # Apply changes to a temp dir to simulate creating a branch
    tmp = tempfile.mkdtemp(prefix='pr_sim_')
    for path, content in changes.items():
        full = os.path.join(tmp, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, 'w', encoding='utf-8') as fh:
            fh.write(content)
    # Return a fake PR URL (in real use we'd call GitHub API)
    return f"https://github.com/demo/repo/pull/{os.path.basename(tmp)}"
