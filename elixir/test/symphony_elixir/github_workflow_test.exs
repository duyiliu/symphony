defmodule SymphonyElixir.GitHub.WorkflowTest do
  use ExUnit.Case, async: false

  alias SymphonyElixir.GitHub.{AgentTool, Workflow, WorkflowPolicy}
  alias SymphonyElixir.Tracker.Issue

  @sha String.duplicate("a", 40)
  @new_sha String.duplicate("b", 40)

  setup do
    dir = Path.join(System.tmp_dir!(), "github-workflow-#{System.unique_integer([:positive])}")

    settings = %{
      kind: "github",
      required_labels: [],
      provider: %{"repo" => "owner/repo", "token" => "test"},
      github_workflow: %{
        "enabled" => true,
        "state_dir" => dir,
        "base_branch" => "main",
        "max_attempts" => 2
      }
    }

    {:ok, remote} = Agent.start_link(fn -> initial_remote() end)

    opts = [
      github_client: fn method, path, params, body, _opts ->
        Agent.get_and_update(remote, fn state ->
          {response, state} = respond(method, path, params, body, state)
          {{:ok, response}, %{state | calls: [{method, path, body} | state.calls]}}
        end)
      end
    ]

    on_exit(fn -> File.rm_rf!(dir) end)
    %{settings: settings, remote: remote, opts: opts}
  end

  test "agent is the only execution switch; needs-human pauses and legacy labels are inert", %{settings: settings} do
    assert WorkflowPolicy.routable?(issue(["agent"]), settings)
    assert WorkflowPolicy.phase(["agent"]) == :develop
    assert WorkflowPolicy.phase(["needs-human"]) == :waiting
    assert WorkflowPolicy.phase(["agent", "needs-human"]) == :invalid
    refute WorkflowPolicy.routable?(issue(["agent", "needs-human"]), settings)
    refute Workflow.routable?(issue(["agent", "needs-human"]), settings)
    # Legacy authorization label carries no meaning.
    assert WorkflowPolicy.phase(["merge-approved"]) == :waiting
    refute WorkflowPolicy.routable?(issue(["merge-approved"]), settings)
    assert WorkflowPolicy.routable?(issue(["agent", "merge-approved"]), settings)
    assert WorkflowPolicy.routable?(issue(["agent", "high-risk"]), settings)

    settings = %{settings | required_labels: ["team", "ready"]}
    refute WorkflowPolicy.routable?(issue(["agent", "team"]), settings)
    assert WorkflowPolicy.routable?(issue(["agent", "team", "ready"]), settings)
    assert Workflow.routable?(issue([]), %{settings | kind: "linear", required_labels: []})
  end

  test "configuration validates storage, base, budgets and risk rules", %{settings: settings} do
    assert :ok = WorkflowPolicy.validate(settings)

    for {key, value} <- [{"state_dir", "/"}, {"base_branch", ""}, {"max_attempts", 0}] do
      assert {:error, _} = WorkflowPolicy.validate(put_in(settings.github_workflow[key], value))
    end

    assert {:error, :invalid_github_workflow_risk} =
             WorkflowPolicy.validate(put_in(settings.github_workflow["risk"], %{"max_auto_files" => 0}))

    assert {:error, :invalid_github_workflow_risk} =
             WorkflowPolicy.validate(put_in(settings.github_workflow["risk"], %{"review_required_paths" => [""]}))

    assert {:error, :invalid_github_workflow_risk} =
             WorkflowPolicy.validate(put_in(settings.github_workflow["risk"], "strict"))

    for label <- ["agent", "needs-human", "high-risk", "merge-approved"] do
      assert {:error, :execution_labels_must_not_be_required_labels} =
               WorkflowPolicy.validate(%{settings | required_labels: [label]})
    end

    risky = put_in(settings.github_workflow["risk"], %{"review_required_paths" => ["db/**"], "max_auto_files" => 7})

    assert :ok = WorkflowPolicy.validate(risky)
    assert WorkflowPolicy.review_required_paths(risky) == ["db/**"]
    assert WorkflowPolicy.max_auto_files(risky) == 7
    assert WorkflowPolicy.review_required_paths(settings) != []
    assert is_integer(WorkflowPolicy.max_auto_files(settings))
  end

  test "raw API policy blocks authorization and mutation bypasses", ctx do
    %{settings: settings, opts: opts, remote: remote} = ctx

    denied = [
      {"POST", "/repos/owner/repo/issues/1/labels", %{"labels" => ["agent"]}},
      {"POST", "/repos/owner/repo/issues/1/labels", %{"labels" => ["needs-human"]}},
      {"PATCH", "/repos/owner/repo/issues/1", %{"labels" => ["merge-approved"]}},
      {"PATCH", "/repos/owner/repo/issues/1", %{"state" => "closed"}},
      {"DELETE", "/repos/owner/repo/issues/1/labels/agent", nil},
      {"PUT", "/repos/owner/repo/pulls/2/merge", %{}},
      {"POST", "/repos/owner/repo/pulls/2/reviews", %{"event" => "APPROVE"}},
      {"POST", "/graphql", %{}},
      {"GET", "/repos/other/repo/issues", nil},
      {"GET", "/repos/owner/repo/issues/../actions/secrets", nil},
      {"GET", "/repos/owner/repo/issues/%2e%2e/actions/secrets", nil}
    ]

    for {method, path, body} <- denied do
      response = AgentTool.execute("github_api", %{"method" => method, "path" => path, "body" => body}, Keyword.put(opts, :tracker_settings, settings))
      refute response["success"]
      assert response["output"] =~ "github_workflow_policy_denied"
    end

    assert Agent.get(remote, & &1.calls) == []
    assert :ok = WorkflowPolicy.allow_request(settings, "POST", "/repos/owner/repo/issues/1/comments", %{"body" => "workpad"})
    assert :ok = WorkflowPolicy.allow_request(%{settings | github_workflow: %{}}, "PUT", "/repos/owner/repo/pulls/2/merge", %{})
  end

  test "delivery is frozen and published while the agent label is kept", ctx do
    assert :ok = Workflow.begin_run(issue(), ctx.settings, ctx.opts)

    assert {:ok, %{"status" => "delivered", "delivery" => %{"sha" => @sha, "published" => true}}} =
             deliver(ctx)

    assert Agent.get(ctx.remote, & &1.labels) == ["agent"]
    calls = Agent.get(ctx.remote, &Enum.reverse(&1.calls))
    comment = Enum.find_index(calls, fn {m, p, _} -> m == "POST" and String.ends_with?(p, "/comments") end)
    assert is_integer(comment)
    refute Enum.any?(calls, fn {m, p, _} -> m == "DELETE" and String.ends_with?(p, "/labels") end)
    refute Enum.any?(calls, fn {m, p, _} -> m == "PUT" and String.ends_with?(p, "/merge") end)
    assert {:ok, %{"delivery" => %{"tests" => "mix test: passed"}}} = call(ctx.settings, "status")
  end

  test "low-risk issue finishes unattended with only the agent label", ctx do
    assert :ok = Workflow.begin_run(issue(), ctx.settings, ctx.opts)
    assert {:ok, _} = deliver(ctx)
    assert Agent.get(ctx.remote, & &1.labels) == ["agent"]

    assert {:ok, %{"status" => "merged"}} = call(ctx.settings, "merge", ctx.opts)
    assert Agent.get(ctx.remote, & &1.merged)
    assert Agent.get(ctx.remote, & &1.issue_state) == "closed"
    assert Agent.get(ctx.remote, & &1.labels) == []
    assert {"PUT", "/repos/owner/repo/pulls/2/merge", %{"sha" => @sha, "merge_method" => "squash"}} in Agent.get(ctx.remote, & &1.calls)
  end

  test "pending checks wait and failing checks request repair instead of pausing", ctx do
    assert :ok = Workflow.begin_run(issue(), ctx.settings, ctx.opts)
    assert {:ok, _} = deliver(ctx)

    Agent.update(ctx.remote, &%{&1 | checks: [%{"id" => 2, "name" => "test", "head_sha" => @sha, "status" => "in_progress", "conclusion" => nil}]})
    assert {:ok, %{"status" => "waiting_checks", "pending" => ["test"]}} = call(ctx.settings, "merge", ctx.opts)

    Agent.update(ctx.remote, &%{&1 | checks: [%{"id" => 3, "name" => "test", "head_sha" => @sha, "status" => "completed", "conclusion" => "failure"}]})
    assert {:ok, %{"status" => "needs_repair", "reason" => reason}} = call(ctx.settings, "merge", ctx.opts)
    assert reason =~ "checks failing"
    assert_no_merge(ctx)

    # Repair: new head on the same PR, fresh delivery, checks green again.
    Agent.update(ctx.remote, &%{&1 | sha: @new_sha, checks: [%{"id" => 4, "name" => "test", "head_sha" => @new_sha, "status" => "completed", "conclusion" => "success"}]})

    assert {:ok, %{"status" => "delivered", "delivery" => %{"sha" => @new_sha}}} =
             Workflow.execute(ctx.settings, %{"action" => "deliver", "issue" => "1", "pr" => 2, "tests" => "repaired"}, ctx.opts)

    assert {:ok, %{"status" => "merged"}} = call(ctx.settings, "merge", ctx.opts)
    assert Agent.get(ctx.remote, & &1.issue_state) == "closed"
  end

  test "no reported checks does not bypass the GitHub merge gate", ctx do
    assert :ok = Workflow.begin_run(issue(), ctx.settings, ctx.opts)
    assert {:ok, _} = deliver(ctx)
    Agent.update(ctx.remote, &%{&1 | checks: [], gate: %{"mergeStateStatus" => "BLOCKED", "reviewDecision" => nil}})
    assert {:ok, %{"status" => "needs_repair"}} = call(ctx.settings, "merge", ctx.opts)
    assert_no_merge(ctx)
    Agent.update(ctx.remote, &%{&1 | gate: %{"mergeStateStatus" => "CLEAN", "reviewDecision" => nil}})
    assert {:ok, %{"status" => "merged"}} = call(ctx.settings, "merge", ctx.opts)
  end

  test "checks discovered from GitHub must all pass", ctx do
    assert :ok = Workflow.begin_run(issue(), ctx.settings, ctx.opts)
    assert {:ok, _} = deliver(ctx)
    Agent.update(ctx.remote, &Map.put(&1, :statuses, [%{"context" => "external-ci", "state" => "pending"}]))
    assert {:ok, %{"status" => "waiting_checks", "pending" => ["external-ci"]}} = call(ctx.settings, "merge", ctx.opts)
    Agent.update(ctx.remote, &Map.put(&1, :statuses, [%{"context" => "external-ci", "state" => "failure"}]))
    assert {:ok, %{"status" => "needs_repair"}} = call(ctx.settings, "merge", ctx.opts)
    assert_no_merge(ctx)
    Agent.update(ctx.remote, &Map.put(&1, :statuses, [%{"context" => "external-ci", "state" => "success"}]))
    assert {:ok, %{"status" => "merged"}} = call(ctx.settings, "merge", ctx.opts)
  end

  test "dirty merge states and requested changes are repaired; unknown waits; reviews are awaited", ctx do
    assert :ok = Workflow.begin_run(issue(), ctx.settings, ctx.opts)
    assert {:ok, _} = deliver(ctx)

    for {gate, status} <- [
          {%{"mergeStateStatus" => "DIRTY", "reviewDecision" => nil}, "needs_repair"},
          {%{"mergeStateStatus" => "BLOCKED", "reviewDecision" => nil}, "needs_repair"},
          {%{"mergeStateStatus" => "CLEAN", "reviewDecision" => "CHANGES_REQUESTED"}, "needs_repair"},
          {%{"mergeStateStatus" => "UNKNOWN", "reviewDecision" => nil}, "waiting_checks"},
          {%{"mergeStateStatus" => "CLEAN", "reviewDecision" => "REVIEW_REQUIRED"}, "waiting_review"}
        ] do
      Agent.update(ctx.remote, &%{&1 | gate: gate})
      assert {:ok, %{"status" => ^status}} = call(ctx.settings, "merge", ctx.opts)
      assert_no_merge(ctx)
    end
  end

  test "risky paths and high-risk labels require a current review approval", ctx do
    assert :ok = Workflow.begin_run(issue(), ctx.settings, ctx.opts)
    assert {:ok, _} = deliver(ctx)

    Agent.update(ctx.remote, &%{&1 | files: [%{"filename" => "db/migrations/001_add_users.ex"}]})
    assert {:ok, %{"status" => "waiting_review"}} = call(ctx.settings, "merge", ctx.opts)
    assert_no_merge(ctx)

    # Stale-commit or author approvals do not authorize the current head.
    Agent.update(
      ctx.remote,
      &%{
        &1
        | reviews: [
            %{"state" => "APPROVED", "commit_id" => String.duplicate("z", 40), "user" => %{"login" => "reviewer", "type" => "User"}},
            %{"state" => "APPROVED", "commit_id" => @sha, "user" => %{"login" => "author", "type" => "User"}}
          ]
      }
    )

    assert {:ok, %{"status" => "waiting_review"}} = call(ctx.settings, "merge", ctx.opts)

    Agent.update(ctx.remote, &%{&1 | reviews: [%{"state" => "APPROVED", "commit_id" => @sha, "user" => %{"login" => "reviewer", "type" => "User"}}]})
    assert {:ok, %{"status" => "merged"}} = call(ctx.settings, "merge", ctx.opts)
  end

  test "the high-risk issue label also requires review approval", ctx do
    Agent.update(ctx.remote, &%{&1 | labels: ["agent", "high-risk"]})
    assert :ok = Workflow.begin_run(issue(["agent", "high-risk"]), ctx.settings, ctx.opts)
    assert {:ok, _} = deliver(ctx)
    assert {:ok, %{"status" => "waiting_review"}} = call(ctx.settings, "merge", ctx.opts)
    assert_no_merge(ctx)
  end

  test "oversized diffs pause for a human instead of guessing", ctx do
    settings = %{ctx.settings | github_workflow: Map.put(ctx.settings.github_workflow, "risk", %{"max_auto_files" => 1})}
    assert :ok = Workflow.begin_run(issue(), settings, ctx.opts)
    assert {:ok, _} = Workflow.execute(settings, %{"action" => "deliver", "issue" => "1", "pr" => 2, "tests" => "passed"}, ctx.opts)
    Agent.update(ctx.remote, &%{&1 | files: [%{"filename" => "a.ex"}, %{"filename" => "b.ex"}]})

    assert {:ok, %{"status" => "paused"}} = call(settings, "merge", ctx.opts)
    assert Agent.get(ctx.remote, & &1.labels) == ["needs-human"]

    assert Enum.any?(Agent.get(ctx.remote, & &1.calls), fn
             {"POST", "/repos/owner/repo/issues/1/comments", %{"body" => body}} -> body =~ "auto-merge budget"
             _ -> false
           end)

    assert_no_merge(ctx)
  end

  test "pause adds needs-human and removes agent; re-adding agent resumes as a new cycle", ctx do
    assert :ok = Workflow.begin_run(issue(), ctx.settings, ctx.opts)
    assert {:ok, _} = deliver(ctx)

    assert {:ok, %{"status" => "paused"}} =
             Workflow.execute(ctx.settings, %{"action" => "pause", "issue" => "1", "reason" => "CI keeps failing: evidence"}, ctx.opts)

    assert Agent.get(ctx.remote, & &1.labels) == ["needs-human"]
    assert {:ok, %{"blocked" => true, "error" => error}} = call(ctx.settings, "status", ctx.opts)
    assert error =~ "CI keeps failing"
    refute WorkflowPolicy.routable?(issue(["needs-human"]), ctx.settings)
    assert Workflow.waiting?(issue(), ctx.settings, ctx.opts)

    # Human resolves and re-labels; the delivery must be registered again.
    human_label(ctx.remote, "agent")
    assert :ok = Workflow.begin_run(issue(), ctx.settings, ctx.opts)

    assert Enum.any?(Agent.get(ctx.remote, & &1.calls), fn
             {"POST", "/repos/owner/repo/issues/1/comments", %{"body" => body}} ->
               body =~ "Manual resume" and body =~ "@human" and body =~ "CI keeps failing" and
                 body =~ "not merge authorization"

             _ ->
               false
           end)

    assert {:ok, %{"status" => "delivered"}} =
             Workflow.execute(ctx.settings, %{"action" => "deliver", "issue" => "1", "pr" => 2, "tests" => "fixed"}, ctx.opts)

    assert {:ok, %{"status" => "merged"}} = call(ctx.settings, "merge", ctx.opts)
    assert Agent.get(ctx.remote, & &1.labels) == []
  end

  test "head movement after delivery fails closed into needs-human", ctx do
    assert :ok = Workflow.begin_run(issue(), ctx.settings, ctx.opts)
    assert {:ok, _} = deliver(ctx)
    Agent.update(ctx.remote, &%{&1 | sha: @new_sha})

    assert {:error, :delivery_changed} = call(ctx.settings, "merge", ctx.opts)
    assert Agent.get(ctx.remote, & &1.labels) == ["needs-human"]
    assert {:ok, %{"blocked" => true}} = call(ctx.settings, "status", ctx.opts)
    assert_no_merge(ctx)
  end

  test "label revocation, conflicting labels and closed issues stop the merge", ctx do
    assert :ok = Workflow.begin_run(issue(), ctx.settings, ctx.opts)
    assert {:ok, _} = deliver(ctx)

    Agent.update(ctx.remote, &%{&1 | labels: ["agent", "needs-human"]})
    assert {:error, :issue_not_executable} = call(ctx.settings, "merge", ctx.opts)

    Agent.update(ctx.remote, &%{&1 | labels: []})
    assert {:error, :issue_not_executable} = call(ctx.settings, "merge", ctx.opts)

    Agent.update(ctx.remote, &%{&1 | labels: ["agent"], issue_state: "closed"})
    assert {:error, :issue_not_executable} = call(ctx.settings, "merge", ctx.opts)
    assert_no_merge(ctx)
  end

  test "merge without a delivery pauses for a human", ctx do
    assert :ok = Workflow.begin_run(issue(), ctx.settings, ctx.opts)
    assert {:error, :missing_delivery} = call(ctx.settings, "merge", ctx.opts)
    assert Agent.get(ctx.remote, & &1.labels) == ["needs-human"]
    assert {:ok, %{"blocked" => true}} = call(ctx.settings, "status", ctx.opts)
    assert_no_merge(ctx)
  end

  test "attempt budget persists across callers and pauses when exhausted", ctx do
    assert :ok = Workflow.begin_run(issue(), ctx.settings, ctx.opts)
    task = Task.async(fn -> Workflow.begin_run(issue(), ctx.settings, ctx.opts) end)
    assert :ok = Task.await(task)
    assert :waiting = Workflow.begin_run(issue(), ctx.settings, ctx.opts)
    assert Agent.get(ctx.remote, & &1.labels) == ["needs-human"]
    assert {:ok, %{"blocked" => true, "error" => error}} = call(ctx.settings, "status", ctx.opts)
    assert error =~ "attempt limit"

    human_label(ctx.remote, "agent")
    assert :ok = Workflow.begin_run(issue(), ctx.settings, ctx.opts)
    assert {:ok, %{"attempts" => 1}} = call(ctx.settings, "status", ctx.opts)
  end

  test "re-dispatch after delivery is budgeted and ends in needs-human", ctx do
    assert :ok = Workflow.begin_run(issue(), ctx.settings, ctx.opts)
    assert {:ok, _} = deliver(ctx)

    Agent.update(ctx.remote, &%{&1 | gate: %{"mergeStateStatus" => "UNKNOWN", "reviewDecision" => nil}})
    assert :ok = Workflow.begin_run(issue(), ctx.settings, ctx.opts)
    assert :waiting = Workflow.begin_run(issue(), ctx.settings, ctx.opts)
    assert Agent.get(ctx.remote, & &1.labels) == ["needs-human"]
    assert_no_merge(ctx)
  end

  test "failed label removal fails closed until a new event without resetting budget", ctx do
    assert :ok = Workflow.begin_run(issue(), ctx.settings, ctx.opts)
    opts = failing_label_removal(ctx)

    assert {:error, {:github_status, 502, _, _}} =
             Workflow.execute(ctx.settings, %{"action" => "pause", "issue" => "1", "reason" => "stop"}, opts)

    # needs-human was added but agent removal failed: the conflicting labels
    # fail closed — no dispatch, still paused, budget untouched.
    assert Agent.get(ctx.remote, & &1.labels) == ["agent", "needs-human"]
    refute Workflow.routable?(issue(["agent", "needs-human"]), ctx.settings)
    assert Workflow.waiting?(issue(), ctx.settings, ctx.opts)

    for _ <- 1..5 do
      assert {:error, :issue_not_executable} = Workflow.begin_run(issue(), ctx.settings, opts)
    end

    assert {:ok, %{"blocked" => true, "attempts" => 1}} = call(ctx.settings, "status", ctx.opts)
    assert :ok = Workflow.record_failure(issue(), ctx.settings, :temporary_failure)

    # Human clears the conflict and re-labels; the same event can be re-used
    # only after a fresh labeled event opens a new cycle.
    Agent.update(ctx.remote, &%{&1 | labels: ["needs-human"]})
    assert Workflow.waiting?(issue(), ctx.settings, ctx.opts)
    human_label(ctx.remote, "agent")
    refute Workflow.waiting?(issue(), ctx.settings, ctx.opts)
    assert :ok = Workflow.begin_run(issue(), ctx.settings, ctx.opts)
  end

  test "ambiguous or unrelated PRs, drafts and unexpected base fail closed", ctx do
    assert :ok = Workflow.begin_run(issue(), ctx.settings, ctx.opts)

    for prs <- [[], [%{"number" => 2, "state" => "OPEN"}, %{"number" => 3, "state" => "OPEN"}]] do
      Agent.update(ctx.remote, &%{&1 | linked: prs})
      assert {:error, :ambiguous_pr_association} = deliver(ctx)
    end

    Agent.update(ctx.remote, &%{&1 | linked: [%{"number" => 2, "state" => "OPEN"}], base: "other"})
    assert {:error, :unexpected_base_branch} = deliver(ctx)
    assert_no_merge(ctx)
  end

  test "unpublished delivery cannot merge; retry publishes and proceeds", ctx do
    assert :ok = Workflow.begin_run(issue(), ctx.settings, ctx.opts)
    client = ctx.opts[:github_client]

    opts = [
      github_client: fn method, path, params, body, options ->
        if method == "POST" and String.ends_with?(path, "/comments") do
          {:ok, %{status: 502, body: %{}}}
        else
          client.(method, path, params, body, options)
        end
      end
    ]

    assert {:error, {:github_status, 502, _, _}} =
             Workflow.execute(ctx.settings, %{"action" => "deliver", "issue" => "1", "pr" => 2, "tests" => "B tested"}, opts)

    assert {:ok, %{"delivery" => %{"published" => false}}} = call(ctx.settings, "status", ctx.opts)
    assert {:error, :delivery_not_published} = call(ctx.settings, "merge", ctx.opts)
    assert Agent.get(ctx.remote, & &1.labels) == ["needs-human"]
    assert_no_merge(ctx)

    # Recovery: human re-adds agent; re-delivery publishes and the merge proceeds.
    human_label(ctx.remote, "agent")
    assert :ok = Workflow.begin_run(issue(), ctx.settings, ctx.opts)
    assert {:ok, %{"delivery" => %{"published" => true}}} = deliver(ctx)
    assert {:ok, %{"status" => "merged"}} = call(ctx.settings, "merge", ctx.opts)
  end

  test "uncertain merge result resolves via GitHub state without repeating the merge", ctx do
    assert :ok = Workflow.begin_run(issue(), ctx.settings, ctx.opts)
    assert {:ok, _} = deliver(ctx)

    Agent.update(ctx.remote, &%{&1 | lose_merge_response: true})
    assert {:error, {:github_status, 502, _, _}} = call(ctx.settings, "merge", ctx.opts)
    assert {:ok, %{"status" => "merged"}} = call(ctx.settings, "merge", ctx.opts)
    assert Agent.get(ctx.remote, & &1.issue_state) == "closed"
    assert Enum.count(Agent.get(ctx.remote, & &1.calls), fn {m, _, _} -> m == "PUT" end) == 1
  end

  test "interrupted finalization is safely resumed by an explicit host call", ctx do
    assert :ok = Workflow.begin_run(issue(), ctx.settings, ctx.opts)
    assert {:ok, _} = deliver(ctx)
    Agent.update(ctx.remote, &Map.put(&1, :fail_close, true))
    assert {:error, {:github_status, 502, _, _}} = call(ctx.settings, "merge", ctx.opts)
    assert {:ok, %{"pending_merge" => true}} = call(ctx.settings, "status", ctx.opts)
    assert {:ok, %{"status" => "merged"}} = call(ctx.settings, "merge", ctx.opts)
    assert {:ok, %{"status" => "merged"}} = call(ctx.settings, "merge", ctx.opts)
    assert Enum.count(Agent.get(ctx.remote, & &1.calls), fn {m, _, _} -> m == "PUT" end) == 1
  end

  test "an unresolved merge intent cannot be overwritten by another delivery", ctx do
    assert :ok = Workflow.begin_run(issue(), ctx.settings, ctx.opts)
    assert {:ok, _} = deliver(ctx)
    Agent.update(ctx.remote, &%{&1 | push_on_merge: true})
    assert {:error, {:github_status, 409, _, _}} = call(ctx.settings, "merge", ctx.opts)
    assert {:error, :merge_result_pending} = deliver(ctx)
    assert {:ok, %{"pending_merge" => true, "delivery" => %{"sha" => @sha}}} = call(ctx.settings, "status", ctx.opts)
  end

  test "workspace overlap and symlink aliases are rejected", ctx do
    root = ctx.settings.github_workflow["state_dir"]
    File.mkdir_p!(root)
    settings = %{tracker: ctx.settings, workspace: %{root: root}}
    assert {:error, :github_workflow_store_overlaps_workspaces} = WorkflowPolicy.validate_runtime(settings)
    assert :ok = WorkflowPolicy.validate_runtime(%{settings | workspace: %{root: root <> "-workspace"}})
    link = Path.join(root, "workspace-alias")
    File.ln_s!(root, link)

    assert {:error, :github_workflow_store_overlaps_workspaces} =
             WorkflowPolicy.validate_runtime(%{settings | workspace: %{root: link}})

    refute WorkflowPolicy.same_phase?(issue(), issue(["needs-human"]), ctx.settings)
    assert WorkflowPolicy.same_phase?(issue(), issue(["agent", "high-risk"]), ctx.settings)
    assert WorkflowPolicy.same_phase?(issue(), issue([]), %{ctx.settings | kind: "linear"})
  end

  test "malformed tool arguments and invalid phases cannot cause mutation", ctx do
    assert {:error, :invalid_github_workflow_issue} = Workflow.execute(ctx.settings, %{"action" => "merge", "issue" => %{}}, ctx.opts)
    assert {:error, :invalid_github_workflow_action} = call(ctx.settings, "approve", ctx.opts)
    assert {:error, :no_active_workflow_run} = deliver(ctx)
    assert {:error, :invalid_arguments} = Workflow.execute(ctx.settings, "merge", ctx.opts)
    assert_no_merge(ctx)
  end

  test "worker failure evidence and budget survive another caller", ctx do
    assert :ok = Workflow.begin_run(issue(), ctx.settings, ctx.opts)
    assert :ok = Workflow.record_failure(issue(), ctx.settings, {:test_failed, "mix test"})
    assert {:ok, %{"attempts" => 1, "last_error" => evidence}} = call(ctx.settings, "status", ctx.opts)
    assert evidence =~ "test_failed"
  end

  test "corrupt receipt never resets budget or permits execution", ctx do
    File.mkdir_p!(ctx.settings.github_workflow["state_dir"])
    File.write!(Path.join(ctx.settings.github_workflow["state_dir"], "1.json"), "broken")
    assert {:error, :invalid_workflow_receipt} = Workflow.begin_run(issue(), ctx.settings, ctx.opts)
    assert_no_merge(ctx)
  end

  test "string issue ids from the poller reach GraphQL variables as integers", ctx do
    assert :ok = Workflow.begin_run(issue(), ctx.settings, ctx.opts)
    assert {:ok, _} = deliver(ctx)
    assert {:ok, %{"status" => "merged"}} = call(ctx.settings, "merge", ctx.opts)

    graphql_bodies =
      Agent.get(ctx.remote, & &1.calls)
      |> Enum.filter(fn {_method, path, _body} -> path == "/graphql" end)
      |> Enum.map(fn {_method, _path, body} -> body end)

    assert graphql_bodies != []

    Enum.each(graphql_bodies, fn %{"variables" => variables} ->
      assert variables != %{}
      assert Enum.any?(Map.values(variables), &is_integer/1)
      refute is_binary(variables["issue"])
      refute is_binary(variables["pr"])
    end)
  end

  test "needs-human and agent together never dispatch", ctx do
    Agent.update(
      ctx.remote,
      &%{
        &1
        | labels: ["agent", "needs-human"],
          events: [
            %{"id" => 1, "event" => "labeled", "label" => %{"name" => "agent"}, "actor" => %{"id" => 10, "type" => "User", "login" => "human"}},
            %{"id" => 2, "event" => "labeled", "label" => %{"name" => "needs-human"}, "actor" => %{"id" => 10, "type" => "User", "login" => "human"}}
          ]
      }
    )

    refute Workflow.routable?(issue(["agent", "needs-human"]), ctx.settings)
    assert {:error, :issue_not_executable} = Workflow.begin_run(issue(["agent", "needs-human"]), ctx.settings, ctx.opts)
    assert_no_merge(ctx)
  end

  test "glob risk patterns match nested and root paths", ctx do
    paths = WorkflowPolicy.review_required_paths(ctx.settings)
    assert Workflow.path_matches?(paths, "db/migrations/001_x.ex")
    assert Workflow.path_matches?(paths, "priv/repo/migrations/001_x.ex")
    assert Workflow.path_matches?(paths, ".github/workflows/ci.yml")
    assert Workflow.path_matches?(paths, "schema.sql")
    assert Workflow.path_matches?(paths, "lib/app_web/controllers/auth_controller.ex")
    refute Workflow.path_matches?(paths, "lib/app.ex")
    refute Workflow.path_matches?(paths, "README.md")
  end

  defp failing_label_removal(ctx) do
    client = ctx.opts[:github_client]

    [
      github_client: fn method, path, params, body, options ->
        if method == "DELETE", do: {:ok, %{status: 502, body: %{}}}, else: client.(method, path, params, body, options)
      end
    ]
  end

  defp call(settings, action, opts \\ []), do: Workflow.execute(settings, %{"action" => action, "issue" => "1"}, opts)
  defp issue(labels \\ ["agent"]), do: %Issue{id: "1", identifier: "GH-1", state: "open", labels: labels, dispatchable: true}

  defp deliver(ctx), do: Workflow.execute(ctx.settings, %{"action" => "deliver", "issue" => "1", "pr" => 2, "tests" => "mix test: passed"}, ctx.opts)

  defp human_label(remote, label, login \\ "human") do
    Agent.update(remote, fn s ->
      id = s.next_event
      actor_id = if login == "automation", do: 20, else: 10
      actor = %{"id" => actor_id, "type" => "User", "login" => login}
      event = %{"id" => id, "event" => "labeled", "label" => %{"name" => label}, "actor" => actor}
      %{s | labels: [label], events: s.events ++ [event], next_event: id + 1}
    end)
  end

  defp assert_no_merge(ctx), do: refute(Enum.any?(Agent.get(ctx.remote, & &1.calls), fn {m, p, _} -> m == "PUT" and String.ends_with?(p, "/merge") end))

  defp initial_remote do
    %{
      labels: ["agent"],
      events: [%{"id" => 1, "event" => "labeled", "label" => %{"name" => "agent"}, "actor" => %{"id" => 10, "type" => "User", "login" => "human"}}],
      next_event: 2,
      sha: @sha,
      base: "main",
      merged: false,
      draft: false,
      issue_state: "open",
      calls: [],
      linked: [%{"number" => 2, "state" => "OPEN"}],
      protected: true,
      gate: %{"mergeStateStatus" => "CLEAN", "reviewDecision" => nil},
      push_on_merge: false,
      lose_merge_response: false,
      files: [%{"filename" => "lib/app.ex"}],
      reviews: [],
      checks: [%{"id" => 1, "name" => "test", "head_sha" => @sha, "status" => "completed", "conclusion" => "success"}]
    }
  end

  defp pr(state),
    do: %{
      "number" => 2,
      "state" => if(state.merged or Map.get(state, :pr_closed, false), do: "closed", else: "open"),
      "draft" => state.draft,
      "merged" => state.merged,
      "user" => %{"login" => "author", "type" => "User"},
      "head" => %{"sha" => state.sha, "repo" => %{"full_name" => "owner/repo"}},
      "base" => %{"ref" => state.base, "repo" => %{"full_name" => "owner/repo"}}
    }

  defp respond("GET", "/user", _, _, s), do: ok(%{"id" => 20, "login" => "automation"}, s)
  defp respond("GET", "/repos/owner/repo", _, _, s), do: ok(%{"permissions" => %{"admin" => Map.get(s, :admin, false)}}, s)
  defp respond("GET", "/repos/owner/repo/issues/1", _, _, s), do: ok(%{"state" => s.issue_state, "labels" => Enum.map(s.labels, &%{"name" => &1})}, s)
  defp respond("GET", "/repos/owner/repo/issues/1/events", _, _, s), do: ok(s.events, s)
  defp respond("GET", "/repos/owner/repo/pulls/2", _, _, s), do: ok(pr(s), s)
  defp respond("GET", "/repos/owner/repo/pulls/2/files", _, _, s), do: ok(s.files, s)
  defp respond("GET", "/repos/owner/repo/pulls/2/reviews", _, _, s), do: ok(s.reviews, s)
  defp respond("GET", "/repos/owner/repo/branches/main", _, _, s), do: ok(%{"protected" => s.protected}, s)

  defp respond("GET", "/repos/owner/repo/commits/" <> path, _, _, s) do
    if String.ends_with?(path, "/check-runs"), do: ok(%{"check_runs" => s.checks}, s), else: ok(Map.get(s, :statuses, []), s)
  end

  defp respond("POST", "/graphql", _, body, s) do
    if body["variables"]["issue"] do
      ok(%{"data" => %{"repository" => %{"issue" => %{"closedByPullRequestsReferences" => %{"nodes" => s.linked, "pageInfo" => %{"hasNextPage" => false}}}}}}, s)
    else
      gate = Map.merge(s.gate, %{"headRefOid" => s.sha, "baseRefName" => s.base})
      ok(%{"data" => %{"repository" => %{"pullRequest" => gate}}}, s)
    end
  end

  defp respond("POST", "/repos/owner/repo/issues/1/labels", _, %{"labels" => [label]}, s), do: ok(%{}, %{s | labels: Enum.uniq(s.labels ++ [label])})
  defp respond("POST", "/repos/owner/repo/issues/1/comments", _, _, s), do: ok(%{"id" => length(s.calls) + 100}, s)
  defp respond("DELETE", "/repos/owner/repo/issues/1/labels/" <> label, _, _, s), do: ok(%{}, %{s | labels: List.delete(s.labels, label)})

  defp respond("PATCH", "/repos/owner/repo/issues/1", _, %{"state" => "closed"}, s) do
    if Map.get(s, :fail_close, false),
      do: {%{status: 502, body: %{}}, Map.put(s, :fail_close, false)},
      else: ok(%{}, %{s | issue_state: "closed"})
  end

  defp respond("PUT", "/repos/owner/repo/pulls/2/merge", _, body, s) do
    cond do
      s.push_on_merge or body["sha"] != s.sha -> {%{status: 409, body: %{}}, %{s | sha: @new_sha}}
      s.lose_merge_response -> {%{status: 502, body: %{}}, %{s | merged: true}}
      true -> ok(%{"merged" => true, "sha" => String.duplicate("c", 40)}, %{s | merged: true})
    end
  end

  defp respond(method, path, _, _, s), do: {%{status: 500, body: %{"unexpected" => "#{method} #{path}"}}, s}
  defp ok(body, state), do: {%{status: 200, body: body}, state}
end
