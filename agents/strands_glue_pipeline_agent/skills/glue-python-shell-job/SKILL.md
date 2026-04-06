---
name: glue-python-shell-job
description: Instructions on how to create, test and deploy Glue Python Shell jobs for data processing and analysis. Use this skill for small-to-medium ETL jobs which do not require incremental processing with built-in state/bookmarks.
---

# Glue Python Shell Job Skill

## Purpose

Use this skill to create a **one-off AWS Glue Python Shell job** for batch-style data processing where the logic is easier to express in plain Python than in Athena SQL, but the workload is still small enough to run comfortably on a **single machine**.

## When to Use This Skill

Use this skill only if all of the following are true:

- the pipeline is a **one-off batch transformation** over existing Glue/Athena data;
- the logic is awkward in Athena SQL but straightforward in plain Python;
- the workload is still suitable for **single-node** execution;
- the job reads a fixed input snapshot and writes a **fresh output dataset**;
- the pipeline does **not** require:
  - incremental processing,
  - state tracking across runs,
  - job bookmarks,
  - merge / upsert / update / delete semantics,
  - triggers or schedules,
  - crawlers,
  - workflows,
  - Spark.

Interpret **small-to-medium** pragmatically: the data should fit a simple single-machine processing model, without distributed compute, and the job should be able to complete in one pass as snapshot-in / snapshot-out.

Do **not** use this skill for datasets that are being continuously updated or for pipelines that need update semantics. This job type should be treated as **write-new-output only**, not as a mutable pipeline component.

## Instructions for Execution

1. **Discover source metadata first**
   - Use Athena/Glue metadata tools to inspect the source tables.
   - For all Athena MCP tool calls, use:
     - `work_group="primary"`
   - For `manage_aws_athena_databases_and_tables`, always use:
     - `catalog_name="AwsDataCatalog"`

2. **Create a Glue Python Shell job only**
   - Set:
     - `job_definition.Command.Name = "pythonshell"`
     - `job_definition.Command.PythonVersion = "3.9"`
   - Prefer `MaxCapacity`.
   - Do not set Spark worker settings such as `WorkerType` or `NumberOfWorkers`.
   - Do not use Spark APIs or objects:
     - no `SparkContext`
     - no `GlueContext`
     - no `DynamicFrame`
     - no `pyspark`
   - Do not require `--JOB_NAME` unless it is explicitly passed in `job_arguments`.

3. **Keep the script plain and dependency-free**
   - Write the script as plain Python.
   - Do not add extra Python dependencies.
   - Do not use `--additional-python-modules`.
   - Do not install wheels or requirements files.
   - Use only the standard runtime as provided by AWS Glue Python Shell.

4. **Use the correct processing pattern**
   - Read from the source dataset.
   - Perform aggregation, transformation, and feature creation.
   - Write results to a **new S3 output location**.
   - Treat each run as self-contained.
   - Do not implement incremental logic, continuation logic, or update-in-place behavior.

5. **Do not set up orchestration**
   - Do not create triggers.
   - Do not create schedules.
   - Do not create workflows.
   - Do not create crawlers.
   - This skill is for a one-off execution only.

6. **Stage the script**
   - Use `athena_list_s3_buckets` to identify a staging location if needed.
   - Use `athena_upload_to_s3` to upload the script.

7. **Test the job before reporting success**
   - Run one test execution via `start-job-run`.
   - Poll `get-job-run` until terminal state.
   - Report success only if the run state is `SUCCEEDED`.
   - If the run fails, call `glue_get_job_run_diagnostics` and include the root-cause logs.
   - If the script requires CLI parameters, ensure `start-job-run` passes all required args.

8. **Register output metadata explicitly**
   - After a successful run, register the output dataset in Athena directly.
   - Create or update the catalog table explicitly.
   - Report the final:
     - `database.table`
     - S3 output location
   - Do not use a crawler for output registration.

9. **Use code interpreter only for analysis support**
   - Use the code interpreter tool for calculations, summaries, and plotting.
   - Never manually transcribe tabular output into Python.
   - Use `athena_query_to_ci_csv` before plotting or statistical analysis in code interpreter.
   - Read from the sandbox file path returned by that tool.

10. **Handle tool failures clearly**
   - If a tool fails, explain the likely AWS configuration issue clearly and continue with best effort.
   - Do not claim completion unless the test run succeeded and Athena metadata was registered.

## Best Practices

- Prefer Athena query execution when the transformation can be expressed cleanly in SQL.
- Use this skill only when plain Python materially simplifies the logic.
- Keep the job small, single-purpose, and deterministic.
- Write outputs to a clearly defined new S3 path.
- Register Athena metadata directly rather than inferring it.
- Return the exact final Athena table name and S3 location.