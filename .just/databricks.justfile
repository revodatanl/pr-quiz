# --- Databricks bundle & workspace recipes ---

profile := env("DATABRICKS_PROFILE", "free")
init_config := ".dev/init-config.json"

# Deploy identity derives from .dev/init-config.json (the template render
# config) so ops recipes can never drift from what `just deploy` deploys.
# Fail-soft on a fresh clone with no .dev/: guard the reads with path_exists
# (just evaluates only the taken branch) and fall back to safe defaults so
# `just --list` works without local dev config.
have_dev_config := path_exists(justfile_directory() / init_config)
project_name := if have_dev_config == "true" { `python -c "import json; print(json.load(open('.dev/init-config.json'))['project_name'])"` } else { "pr-quiz" }
warehouse_id := if have_dev_config == "true" { `python -c "import json; print(json.load(open('.dev/init-config.json'))['warehouse_id'])"` } else { "" }
app_name := project_name
job_name := project_name + "-generator"

build_dir := ".build"
bundle_dir := build_dir / project_name

# Fail early with a helpful message if the local dev config is missing
[private]
require-dev-config:
  test -f {{init_config}} || { echo "copy .dev/init-config.example.json to .dev/init-config.json and fill in your workspace values" >&2; exit 1; }

# Render the init template into .build/ with this repo's live values (dogfoods the template on every deploy)
[script]
[group: 'databricks']
render: require-dev-config
  rm -rf {{build_dir}};
  find template -type d -name __pycache__ -prune -exec rm -rf {} +;
  databricks bundle init . --config-file {{init_config}} --output-dir {{build_dir}} --profile {{profile}};
  # own git repo: otherwise this repo's .gitignore ({{build_dir}}/) makes bundle
  # sync see zero files and deploy an empty bundle
  git -C {{build_dir}} init -q;
  echo "rendered bundle: {{bundle_dir}}/"

# Validate the bundle configuration (renders the template first)
[script]
[group: 'databricks']
validate: render
  cd {{bundle_dir}};
  databricks bundle validate --profile {{profile}}

# Render + deploy the bundle (schema, job, app resource) to the free workspace
[script]
[group: 'databricks']
deploy: render
  cd {{bundle_dir}};
  databricks bundle deploy --profile {{profile}}

# Push app source to app compute and (re)start it (renders the template first)
[script]
[group: 'databricks']
deploy-app: render
  cd {{bundle_dir}};
  databricks bundle run quiz_app --profile {{profile}}

# Start the app compute if stopped (idempotent, tolerates already-running)
[script]
[group: 'databricks']
start-app:
  if databricks apps start {{app_name}} --profile {{profile}} 2>&1 | grep -qiE "already|ACTIVE"; then
    echo "app already running";
  fi
  url=$(databricks apps get {{app_name}} -o json --profile {{profile}} | python -c "import json,sys; print(json.load(sys.stdin).get('url',''))");
  echo "app: $url"

# Trigger quiz generation for a PR head commit: just run-job 3 abc123 [owner/repo]
# repo defaults to this repo so the dogfood stays one-command; generate_quiz.py
# hard-exits on an empty repo, so it must be in the job_parameters payload.
[script]
[group: 'databricks']
run-job pr_number head_sha repo=repo: require-dev-config
  job_id=$(databricks jobs list --name {{job_name}} --profile {{profile}} -o json | python -c "import json,sys; print(json.load(sys.stdin)[0]['job_id'])");
  echo "job_id=$job_id  pr=$1  sha=$2  repo=$3";
  databricks jobs run-now --json "{\"job_id\":$job_id,\"job_parameters\":{\"pr_number\":\"$1\",\"head_sha\":\"$2\",\"repo\":\"$3\"}}" --profile {{profile}}

# Evaluate the merge gate locally for a commit SHA: just gate-check owner/repo abc123
[group: 'databricks']
gate-check repo head_sha: require-dev-config
  python actions/gate-check/gate_check.py --sha "$2" --repo "$1" --warehouse-id {{warehouse_id}} --profile {{profile}}

# Show recent app logs (CLI v1.5+; /logz in the browser shows the live tail)
[group: 'databricks']
app-logs:
  databricks apps logs {{app_name}} --profile {{profile}}

# Show the latest app deployment: state, failure message, creator, source path
[group: 'databricks']
app-deploy-status:
  databricks apps list-deployments {{app_name}} -o json --profile {{profile}} | python -c "import json,sys; d=json.load(sys.stdin)[0]; print(d['status']['state']); print(d['status'].get('message','')); print('creator:', d['creator']); print('source:', d['source_code_path'])"

# Show a job run's state and task log output: just run-output 832297551353844
[script]
[group: 'databricks']
run-output run_id:
  task_run_id=$(databricks jobs get-run "$1" -o json --profile {{profile}} | python -c "import json,sys; d=json.load(sys.stdin); print(d['state'].get('life_cycle_state',''), d['state'].get('result_state',''), file=sys.stderr); print(d['tasks'][0]['run_id'])");
  databricks jobs get-run-output "$task_run_id" -o json --profile {{profile}} | python -c "import json,sys; d=json.load(sys.stdin); print(d.get('logs') or d.get('error') or json.dumps(d, indent=2))"
