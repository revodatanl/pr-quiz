# --- GitHub gate & PR recipes ---

repo := "revodatanl/pr-quiz"

# Enable branch protection: required quiz-gate status + enforce admins (go-live).
# app_id -1 = status may come from any source: the Streamlit app posts it with a
# PAT (user identity), the workflows post it as the github-actions app.
[script]
[group: 'github']
protect:
  export GH_TOKEN=$(printf 'protocol=https\nhost=github.com\n' | git credential fill | grep '^password=' | cut -d= -f2);
  echo '{"required_status_checks":{"strict":false,"checks":[{"context":"quiz-gate","app_id":-1}]},"enforce_admins":true,"required_pull_request_reviews":null,"restrictions":null}' \
    | gh api -X PUT repos/{{repo}}/branches/main/protection --input - > /dev/null;
  gh api repos/{{repo}}/branches/main/protection --jq '{checks: .required_status_checks.checks, enforce_admins: .enforce_admins.enabled}'

# Remove branch protection from main
[confirm: 'Remove branch protection from main?']
[script]
[group: 'github']
unprotect:
  export GH_TOKEN=$(printf 'protocol=https\nhost=github.com\n' | git credential fill | grep '^password=' | cut -d= -f2);
  gh api -X DELETE repos/{{repo}}/branches/main/protection;
  echo "protection removed"

# Trigger quiz generation on a PR by commenting /quiz: just quiz 3
[script]
[group: 'github']
quiz pr_number:
  export GH_TOKEN=$(printf 'protocol=https\nhost=github.com\n' | git credential fill | grep '^password=' | cut -d= -f2);
  MSYS_NO_PATHCONV=1 gh pr comment "$1" --repo {{repo}} --body "/quiz"

# Re-evaluate the merge gate on a PR by commenting /quiz-check: just quiz-check 3
[script]
[group: 'github']
quiz-check pr_number:
  export GH_TOKEN=$(printf 'protocol=https\nhost=github.com\n' | git credential fill | grep '^password=' | cut -d= -f2);
  MSYS_NO_PATHCONV=1 gh pr comment "$1" --repo {{repo}} --body "/quiz-check"

# (Re)create fixture PRs for quiz E2E testing: just fixture-prs [small medium large generated deleted waived]
[script]
[group: 'github']
fixture-prs *names:
  export GH_TOKEN=$(printf 'protocol=https\nhost=github.com\n' | git credential fill | grep '^password=' | cut -d= -f2);
  if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "working tree not clean; commit or stash first" >&2; exit 1;
  fi
  names="$*"; [ -n "$names" ] || names="small medium large generated deleted waived";
  start=$(git rev-parse --abbrev-ref HEAD);
  tmp=$(mktemp -d);
  # copied out of the tree because the loop switches branches under it; cygpath
  # because native python cannot resolve the POSIX path mktemp prints
  if command -v cygpath > /dev/null; then tmp=$(cygpath -m "$tmp"); fi
  cp scripts/gen_fixture_pr.py "$tmp/";
  git fetch origin main;
  for name in $names; do
    branch="fixture/$name";
    # a PR diff only shows a file as DELETED if it exists on the base, so this
    # fixture branches off fixture/medium, keeps its files, and targets it
    if [ "$name" = "deleted" ]; then
      base="fixture/medium";
      git fetch origin "$base" || { echo "run 'just fixture-prs medium' first" >&2; exit 1; };
      git switch -C "$branch" --no-track "origin/$base";
    else
      base="main";
      git switch -C "$branch" --no-track origin/main;
      rm -rf fixtures/sandbox;
    fi;
    python "$tmp/gen_fixture_pr.py" "$name";
    git add -A fixtures/sandbox;
    git commit -m "test: $name fixture for quiz E2E";
    git push -f origin "$branch";
    if [ "$(gh pr list --repo {{repo}} --head "$branch" --state open --json number --jq length)" = "0" ]; then
      MSYS_NO_PATHCONV=1 gh pr create --repo {{repo}} --head "$branch" --base "$base" \
        --title "Fixture: $name quiz E2E" --body "Deterministic $name fixture. Comment /quiz to test.";
    fi;
  done;
  git switch "$start"

# Close open fixture PRs (fixture/* heads only) and delete their branches
[script]
[group: 'github']
fixture-clean:
  export GH_TOKEN=$(printf 'protocol=https\nhost=github.com\n' | git credential fill | grep '^password=' | cut -d= -f2);
  case "$(git rev-parse --abbrev-ref HEAD)" in fixture/*) git switch main;; esac
  gh pr list --repo {{repo}} --state open --json number,headRefName \
    --jq '.[] | select(.headRefName | startswith("fixture/")) | .number' \
  | while read -r n; do
      echo "closing fixture PR #$n";
      gh pr close "$n" --repo {{repo}} --delete-branch;
    done;
  for b in $(git branch --list 'fixture/*' --format='%(refname:short)'); do
    git branch -D "$b";
  done
