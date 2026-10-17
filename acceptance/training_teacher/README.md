# Independent training-teacher acceptance

Exam owner: `/root/quota_evaluation_qa`. Builders must not edit this directory.

Run the 49 source/staged pre-import methods from the standalone repository:

```sh
python -B acceptance/training_teacher/run_exams.py /absolute/private/report.json
```

Run the four full staged-main methods with the unregistered fixed candidate release:

```sh
/Users/velox/.local/share/cortados-workers/releases/training-teacher-v1-20261017/venv/bin/python -B acceptance/training_teacher/installed_driver.py
```

Set `CORTADOS_TRAINING_TEACHER_STAGE` to the guarded staged checkout. The default is
`/Users/velox/.cortados-builds/training-teacher-activation`. The installed driver must run with
PYTHONPATH unset. It checks actual fixed interpreter and module origins; no production import
bypass is added. Existing common answer validation is used with private DATA and inert usage.
Broker ensure/RPC and Claude execution are the only external boundaries replaced. No provider
quota query, authentication refresh, model turn or live label write is part of these exams.

The synthetic broker retains UUIDs and exact arguments and records native-start counts. It emits
existing public response/result shapes; its success is not evidence of model quality or actual
native containment. Existing accumulated worker/containment/native-profile exams supply those
checks, unchanged. Missing new owners and policy fields are expected prebuild failures, never
skips. Subtest failure/error record counts differ from method counts.

The source/staged tests, installed driver, full-inventory runner, parity and performance scripts
are all required. Root runs historical 006 stages unchanged and the new 007 stage separately.
`METHOD-MAP.json`, `FREEZE.json` and evidence files pin source identities and commands.
