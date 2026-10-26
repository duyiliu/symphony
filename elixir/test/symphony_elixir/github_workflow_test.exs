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
        "isolated_agent" => true,
        "state_dir" => dir,
        "base_branch" => "main",
        "automation_login" => "automation",
        "required_checks" => ["test"],
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

  test "routing is GitHub-only, XOR labels, required_labels retain AND semantics", %{settings: settings} do
    for labels <- [["agent"], ["merge-approved"]] do
      assert WorkflowPolicy.routable?(issue(labels), settings)
    end

    refute WorkflowPolicy.routable?(issue([]), settings)
    refute WorkflowPolicy.routable?(issue(["agent", "merge-approved"]), settings)
    refute Workflow.routable?(issue(["agent", "merge-approved"]), settings)
    settings = %{settings | required_labels: ["team", "ready"]}
    refute WorkflowPolicy.routable?(issue(["agent", "team"]), settings)
    assert WorkflowPolicy.routable?(issue(["agent", "team", "ready"]), settings)
    assert Workflow.routable?(issue([]), %{settings | kind: "linear", required_labels: []})
  end

  test "configuration fails closed without isolation, identity, private storage or checks", %{settings: settings} do
    assert :ok = WorkflowPolicy.validate(settings)

    for {key, value} <- [{"isolated_agent", false}, {"state_dir", "/"}, {"base_branch", ""}, {"automation_login", ""}, {"required_checks", []}, {"max_attempts", 0}] do
      assert {:error, _} = WorkflowPolicy.validate(put_in(settings.github_workflow[key], value))
    end

    with_execution_label = %{settings | required_labels: ["agent"]}
    assert {:error, :execution_labels_must_not_be_required_labels} = WorkflowPolicy.validate(with_execution_label)
  end

  test "raw API policy blocks authorization and mutation bypasses", ctx do
    %{settings: settings, opts: opts, remote: remote} = ctx

    denied = [
      {"POST", "/repos/owner/repo/issues/1/labels", %{"labels" => ["merge-approved"]}},
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

  test "delivery is frozen by host and handed off before labels are removed", ctx do
    %{settings: settings, opts: opts, remote: remote} = ctx
    assert :ok = Workflow.begin_run(issue(), settings, opts)
    assert {:ok, %{"status" => "waiting", "delivery" => %{"sha" => @sha}}} = deliver(ctx)
    assert Agent.get(remote, & &1.labels) == []
    calls = Agent.get(remote, &Enum.reverse(&1.calls))
    comment = Enum.find_index(calls, fn {m, p, _} -> m == "POST" and String.ends_with?(p, "/comments") end)
    remove = Enum.find_index(calls, fn {m, p, _} -> m == "DELETE" and String.ends_with?(p, "/labels/agent") end)
    assert comment < remove
    refute Enum.any?(calls, fn {m, p, _} -> m == "PUT" and String.ends_with?(p, "/merge") end)
    assert {:ok, %{"delivery" => %{"tests" => "mix test: passed"}}} = call(settings, "status")
  end

  test "valid human label merges only approved SHA and finishes issue", ctx do
    prepare_merge(ctx)
    assert :ok = Workflow.begin_run(issue(["merge-approved"]), ctx.settings, ctx.opts)
    assert {:ok, %{"status" => "merged"}} = call(ctx.settings, "merge", ctx.opts)
    assert Agent.get(ctx.remote, & &1.merged)
    assert Agent.get(ctx.remote, & &1.issue_state) == "closed"
    assert Agent.get(ctx.remote, & &1.labels) == []
    assert {"PUT", "/repos/owner/repo/pulls/2/merge", %{"sha" => @sha, "merge_method" => "squash"}} in Agent.get(ctx.remote, & &1.calls)
  end

  test "missing delivery and automation-added labels are rejected", ctx do
    human_label(ctx.remote, "merge-approved")
    assert {:error, :missing_delivery} = Workflow.begin_run(issue(["merge-approved"]), ctx.settings, ctx.opts)
    assert_no_merge(ctx)
    human_label(ctx.remote, "agent")
    assert :ok = Workflow.begin_run(issue(), ctx.settings, ctx.opts)
    assert {:ok, _} = deliver(ctx)
    human_label(ctx.remote, "merge-approved", "automation")
    assert {:error, :automation_cannot_authorize} = Workflow.begin_run(issue(["merge-approved"]), ctx.settings, ctx.opts)
    assert_no_merge(ctx)
  end

  test "head changes before or after admission invalidate authorization", ctx do
    prepare_merge(ctx)
    Agent.update(ctx.remote, &%{&1 | sha: @new_sha})
    assert {:error, :delivery_changed} = Workflow.begin_run(issue(["merge-approved"]), ctx.settings, ctx.opts)
    assert_no_merge(ctx)
  end

  test "label revocation after admission blocks merge", ctx do
    prepare_merge(ctx)
    assert :ok = Workflow.begin_run(issue(["merge-approved"]), ctx.settings, ctx.opts)
    Agent.update(ctx.remote, &%{&1 | labels: []})
    assert {:error, :issue_not_executable} = call(ctx.settings, "merge", ctx.opts)
    assert_no_merge(ctx)
  end

  test "new push during final GitHub request is rejected by expected SHA", ctx do
    prepare_merge(ctx)
    assert :ok = Workflow.begin_run(issue(["merge-approved"]), ctx.settings, ctx.opts)
    Agent.update(ctx.remote, &%{&1 | push_on_merge: true})
    assert {:error, {:github_status, 409, _, _}} = call(ctx.settings, "merge", ctx.opts)
    refute Agent.get(ctx.remote, & &1.merged)
    assert Agent.get(ctx.remote, & &1.issue_state) == "open"
  end

  test "GitHub review gates, unknown mergeability and unprotected base block merge", ctx do
    prepare_merge(ctx)
    assert :ok = Workflow.begin_run(issue(["merge-approved"]), ctx.settings, ctx.opts)

    for gate <- [
          %{"mergeStateStatus" => "UNKNOWN", "reviewDecision" => nil},
          %{"mergeStateStatus" => "BLOCKED", "reviewDecision" => "REVIEW_REQUIRED"},
          %{"mergeStateStatus" => "CLEAN", "reviewDecision" => "CHANGES_REQUESTED"}
        ] do
      Agent.update(ctx.remote, &%{&1 | gate: gate})
      assert {:ok, %{"status" => "waiting_for_checks"}} = call(ctx.settings, "merge", ctx.opts)
    end

    Agent.update(ctx.remote, &%{&1 | protected: false})
    assert {:error, :unprotected_target_branch} = call(ctx.settings, "merge", ctx.opts)
    assert_no_merge(ctx)
  end

  test "repair delivers a new SHA and cannot inherit existing approval", ctx do
    prepare_merge(ctx)
    assert :ok = Workflow.begin_run(issue(["merge-approved"]), ctx.settings, ctx.opts)
    assert {:error, :merge_delivery_requires_new_head} = deliver(ctx)
    Agent.update(ctx.remote, &%{&1 | sha: @new_sha})
    assert {:ok, %{"delivery" => %{"sha" => @new_sha}, "status" => "waiting"}} = deliver(ctx)
    assert Agent.get(ctx.remote, & &1.labels) == []
    assert_no_merge(ctx)
    human_label(ctx.remote, "merge-approved")
    assert :ok = Workflow.begin_run(issue(["merge-approved"]), ctx.settings, ctx.opts)
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

  test "automation identity and administrator accounts cannot authorize merge", ctx do
    prepare_merge(ctx)
    wrong_identity = put_in(ctx.settings, [:github_workflow, "automation_login"], "different-bot")
    assert {:error, :automation_identity_mismatch} = Workflow.begin_run(issue(["merge-approved"]), wrong_identity, ctx.opts)
    human_label(ctx.remote, "merge-approved")
    Agent.update(ctx.remote, &Map.put(&1, :admin, true))
    assert {:error, :automation_must_not_be_admin} = Workflow.begin_run(issue(["merge-approved"]), ctx.settings, ctx.opts)
    assert_no_merge(ctx)
  end

  test "a label already present at delivery cannot approve that delivery", ctx do
    prepare_merge(ctx)
    event_id = Agent.get(ctx.remote, &List.last(&1.events)["id"])
    mutate_record(ctx.settings, &put_in(&1, ["delivery", "previous_approvals"], [event_id]))
    assert {:error, :authorization_predates_delivery} = Workflow.begin_run(issue(["merge-approved"]), ctx.settings, ctx.opts)
    assert_no_merge(ctx)
  end

  test "new head after admission is rejected and merge-queue/dirty states wait", ctx do
    prepare_merge(ctx)
    assert :ok = Workflow.begin_run(issue(["merge-approved"]), ctx.settings, ctx.opts)
    Agent.update(ctx.remote, &%{&1 | gate: %{"mergeStateStatus" => "DIRTY", "reviewDecision" => nil}})
    assert {:ok, %{"status" => "waiting_for_checks"}} = call(ctx.settings, "merge", ctx.opts)
    Agent.update(ctx.remote, &%{&1 | sha: @new_sha})
    assert {:error, :delivery_changed} = call(ctx.settings, "merge", ctx.opts)
    assert_no_merge(ctx)
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

    refute WorkflowPolicy.same_phase?(issue(), issue(["merge-approved"]), ctx.settings)
    assert WorkflowPolicy.same_phase?(issue(), issue(["merge-approved"]), %{ctx.settings | kind: "linear"})
  end

  test "malformed tool arguments and invalid phases cannot cause mutation", ctx do
    assert {:error, :invalid_github_workflow_issue} = Workflow.execute(ctx.settings, %{"action" => "merge", "issue" => %{}}, ctx.opts)
    assert {:error, :invalid_github_workflow_action} = call(ctx.settings, "approve", ctx.opts)
    assert {:error, :not_a_merge_run} = call(ctx.settings, "merge", ctx.opts)
    assert {:error, :no_active_workflow_run} = deliver(ctx)
    assert_no_merge(ctx)
  end

  test "attempt budget persists across callers; new event starts a new budget", ctx do
    assert :ok = Workflow.begin_run(issue(), ctx.settings, ctx.opts)
    task = Task.async(fn -> Workflow.begin_run(issue(), ctx.settings, ctx.opts) end)
    assert :ok = Task.await(task)
    assert :waiting = Workflow.begin_run(issue(), ctx.settings, ctx.opts)
    assert Agent.get(ctx.remote, & &1.labels) == []
    human_label(ctx.remote, "agent")
    assert :ok = Workflow.begin_run(issue(), ctx.settings, ctx.opts)
    assert {:ok, %{"attempts" => 1}} = call(ctx.settings, "status", ctx.opts)
  end

  test "pending or failed checks wait without merging", ctx do
    prepare_merge(ctx)
    assert :ok = Workflow.begin_run(issue(["merge-approved"]), ctx.settings, ctx.opts)

    for checks <- [[], [%{"id" => 2, "name" => "test", "head_sha" => @sha, "status" => "completed", "conclusion" => "failure"}]] do
      Agent.update(ctx.remote, &%{&1 | checks: checks})
      assert {:ok, %{"status" => "waiting_for_checks"}} = call(ctx.settings, "merge", ctx.opts)
    end

    assert_no_merge(ctx)
  end

  test "lost merge response resolves on next call without repeating merge", ctx do
    prepare_merge(ctx)
    assert :ok = Workflow.begin_run(issue(["merge-approved"]), ctx.settings, ctx.opts)
    Agent.update(ctx.remote, &%{&1 | lose_merge_response: true})
    assert {:error, {:github_status, 502, _, _}} = call(ctx.settings, "merge", ctx.opts)
    assert {:ok, %{"status" => "merged"}} = call(ctx.settings, "merge", ctx.opts)
    assert Agent.get(ctx.remote, & &1.issue_state) == "closed"
    assert Enum.count(Agent.get(ctx.remote, & &1.calls), fn {m, _, _} -> m == "PUT" end) == 1
  end

  test "uncertain unmerged result pauses instead of repeating merge", ctx do
    prepare_merge(ctx)
    assert :ok = Workflow.begin_run(issue(["merge-approved"]), ctx.settings, ctx.opts)
    Agent.update(ctx.remote, &%{&1 | push_on_merge: true})
    assert {:error, {:github_status, 409, _, _}} = call(ctx.settings, "merge", ctx.opts)
    assert {:error, :merge_not_confirmed} = call(ctx.settings, "merge", ctx.opts)
    assert {:ok, %{"blocked" => true}} = call(ctx.settings, "status", ctx.opts)
    assert Agent.get(ctx.remote, & &1.issue_state) == "open"
  end

  test "corrupt receipt never resets budget or permits execution", ctx do
    File.mkdir_p!(ctx.settings.github_workflow["state_dir"])
    File.write!(Path.join(ctx.settings.github_workflow["state_dir"], "1.json"), "broken")
    assert {:error, :invalid_workflow_receipt} = Workflow.begin_run(issue(), ctx.settings, ctx.opts)
    assert_no_merge(ctx)
  end

  test "worker failure evidence and budget survive another caller", ctx do
    assert :ok = Workflow.begin_run(issue(), ctx.settings, ctx.opts)
    assert :ok = Workflow.record_failure(issue(), ctx.settings, {:test_failed, "mix test"})
    assert {:ok, %{"attempts" => 1, "last_error" => evidence}} = call(ctx.settings, "status", ctx.opts)
    assert evidence =~ "test_failed"
  end

  test "interrupted finalization is safely resumed by an explicit host call", ctx do
    prepare_merge(ctx)
    assert :ok = Workflow.begin_run(issue(["merge-approved"]), ctx.settings, ctx.opts)
    Agent.update(ctx.remote, &Map.put(&1, :fail_close, true))
    assert {:error, {:github_status, 502, _, _}} = call(ctx.settings, "merge", ctx.opts)
    assert {:ok, %{"pending_merge" => true}} = call(ctx.settings, "status", ctx.opts)
    assert {:ok, %{"status" => "merged"}} = call(ctx.settings, "merge", ctx.opts)
    assert {:ok, %{"status" => "merged"}} = call(ctx.settings, "merge", ctx.opts)
    assert Enum.count(Agent.get(ctx.remote, & &1.calls), fn {m, _, _} -> m == "PUT" end) == 1
  end

  test "handoff preserves a newer human label event", ctx do
    assert :ok = Workflow.begin_run(issue(), ctx.settings, ctx.opts)
    client = ctx.opts[:github_client]

    opts = [
      github_client: fn method, path, params, body, options ->
        result = client.(method, path, params, body, options)
        if method == "POST" and String.ends_with?(path, "/comments"), do: human_label(ctx.remote, "agent")
        result
      end
    ]

    assert {:ok, _} = Workflow.execute(ctx.settings, %{"action" => "deliver", "issue" => "1", "pr" => 2, "tests" => "passed"}, opts)
    assert Agent.get(ctx.remote, & &1.labels) == ["agent"]
    assert :ok = Workflow.begin_run(issue(), ctx.settings, ctx.opts)
  end

  test "failed delivery publication cannot receive a new label authorization", ctx do
    prepare_merge(ctx)
    human_label(ctx.remote, "agent")
    Agent.update(ctx.remote, &%{&1 | sha: @new_sha})
    assert :ok = Workflow.begin_run(issue(), ctx.settings, ctx.opts)
    client = ctx.opts[:github_client]

    opts = [
      github_client: fn method, path, params, body, options ->
        if method == "POST" and String.ends_with?(path, "/comments") do
          human_label(ctx.remote, "merge-approved")
          {:ok, %{status: 502, body: %{}}}
        else
          client.(method, path, params, body, options)
        end
      end
    ]

    assert {:error, {:github_status, 502, _, _}} =
             Workflow.execute(ctx.settings, %{"action" => "deliver", "issue" => "1", "pr" => 2, "tests" => "B tested"}, opts)

    assert {:ok, %{"delivery" => %{"published" => false}}} = call(ctx.settings, "status", ctx.opts)
    assert {:error, :delivery_not_published} = Workflow.begin_run(issue(["merge-approved"]), ctx.settings, ctx.opts)
    assert_no_merge(ctx)
  end

  test "label added during successful publication must be added again", ctx do
    assert :ok = Workflow.begin_run(issue(), ctx.settings, ctx.opts)
    client = ctx.opts[:github_client]

    opts = [
      github_client: fn method, path, params, body, options ->
        result = client.(method, path, params, body, options)
        if method == "POST" and String.ends_with?(path, "/comments"), do: human_label(ctx.remote, "merge-approved")
        result
      end
    ]

    assert {:ok, %{"delivery" => %{"published" => true}}} =
             Workflow.execute(ctx.settings, %{"action" => "deliver", "issue" => "1", "pr" => 2, "tests" => "passed"}, opts)

    assert {:error, :authorization_predates_delivery} = Workflow.begin_run(issue(["merge-approved"]), ctx.settings, ctx.opts)
    assert Agent.get(ctx.remote, & &1.labels) == []
    human_label(ctx.remote, "merge-approved")
    assert :ok = Workflow.begin_run(issue(["merge-approved"]), ctx.settings, ctx.opts)
    assert_no_merge(ctx)
  end

  test "failed label removal waits until a new event without resetting budget", ctx do
    assert :ok = Workflow.begin_run(issue(), ctx.settings, ctx.opts)
    opts = failing_label_removal(ctx)
    assert {:error, {:github_status, 502, _, _}} = Workflow.execute(ctx.settings, %{"action" => "pause", "issue" => "1"}, opts)
    for _ <- 1..5, do: assert(:waiting == Workflow.begin_run(issue(), ctx.settings, opts))
    assert Workflow.waiting?(issue(), ctx.settings, ctx.opts)
    assert {:ok, %{"blocked" => true, "attempts" => 1}} = call(ctx.settings, "status", ctx.opts)
    # A normal comment or worker restart leaves the same label event blocked.
    assert :ok = Workflow.record_failure(issue(), ctx.settings, :temporary_failure)
    assert Workflow.waiting?(issue(), ctx.settings, ctx.opts)
    human_label(ctx.remote, "agent")
    refute Workflow.waiting?(issue(), ctx.settings, ctx.opts)
    assert :ok = Workflow.begin_run(issue(), ctx.settings, ctx.opts)
  end

  test "handoff cleanup failure exhausts budget and does not republish", ctx do
    assert :ok = Workflow.begin_run(issue(), ctx.settings, ctx.opts)
    opts = failing_label_removal(ctx)
    args = %{"action" => "deliver", "issue" => "1", "pr" => 2, "tests" => "passed"}
    assert {:error, {:github_status, 502, _, _}} = Workflow.execute(ctx.settings, args, opts)
    assert {:error, {:github_status, 502, _, _}} = Workflow.begin_run(issue(), ctx.settings, opts)
    assert {:error, {:github_status, 502, _, _}} = Workflow.begin_run(issue(), ctx.settings, opts)
    assert :waiting = Workflow.begin_run(issue(), ctx.settings, opts)
    assert {:ok, %{"attempts" => 2, "blocked" => true}} = call(ctx.settings, "status", ctx.opts)
    comments = Enum.filter(Agent.get(ctx.remote, & &1.calls), fn {m, p, _} -> m == "POST" and String.ends_with?(p, "/comments") end)
    assert length(comments) == 1
  end

  test "an unresolved merge intent cannot be overwritten by another delivery", ctx do
    prepare_merge(ctx)
    assert :ok = Workflow.begin_run(issue(["merge-approved"]), ctx.settings, ctx.opts)
    Agent.update(ctx.remote, &%{&1 | push_on_merge: true})
    assert {:error, {:github_status, 409, _, _}} = call(ctx.settings, "merge", ctx.opts)
    assert {:error, :merge_result_pending} = deliver(ctx)
    assert {:ok, %{"pending_merge" => true, "delivery" => %{"sha" => @sha}}} = call(ctx.settings, "status", ctx.opts)
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

  defp prepare_merge(ctx) do
    assert :ok = Workflow.begin_run(issue(), ctx.settings, ctx.opts)
    assert {:ok, _} = deliver(ctx)
    human_label(ctx.remote, "merge-approved")
  end

  defp human_label(remote, label, login \\ "human") do
    Agent.update(remote, fn s ->
      id = s.next_event
      actor_id = if login == "automation", do: 20, else: 10
      actor = %{"id" => actor_id, "type" => "User", "login" => login}
      event = %{"id" => id, "event" => "labeled", "label" => %{"name" => label}, "actor" => actor}
      %{s | labels: [label], events: s.events ++ [event], next_event: id + 1}
    end)
  end

  defp mutate_record(settings, fun) do
    path = Path.join(settings.github_workflow["state_dir"], "1.json")
    record = path |> File.read!() |> Jason.decode!()
    File.write!(path, Jason.encode!(fun.(record)))
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
      checks: [%{"id" => 1, "name" => "test", "head_sha" => @sha, "status" => "completed", "conclusion" => "success"}]
    }
  end

  defp pr(state),
    do: %{
      "number" => 2,
      "state" => if(state.merged or Map.get(state, :pr_closed, false), do: "closed", else: "open"),
      "draft" => state.draft,
      "merged" => state.merged,
      "head" => %{"sha" => state.sha, "repo" => %{"full_name" => "owner/repo"}},
      "base" => %{"ref" => state.base, "repo" => %{"full_name" => "owner/repo"}}
    }

  defp respond("GET", "/user", _, _, s), do: ok(%{"id" => 20, "login" => "automation"}, s)
  defp respond("GET", "/repos/owner/repo", _, _, s), do: ok(%{"permissions" => %{"admin" => Map.get(s, :admin, false)}}, s)
  defp respond("GET", "/repos/owner/repo/issues/1", _, _, s), do: ok(%{"state" => s.issue_state, "labels" => Enum.map(s.labels, &%{"name" => &1})}, s)
  defp respond("GET", "/repos/owner/repo/issues/1/events", _, _, s), do: ok(s.events, s)
  defp respond("GET", "/repos/owner/repo/pulls/2", _, _, s), do: ok(pr(s), s)
  defp respond("GET", "/repos/owner/repo/branches/main", _, _, s), do: ok(%{"protected" => s.protected}, s)

  defp respond("GET", "/repos/owner/repo/commits/" <> path, _, _, s) do
    if String.ends_with?(path, "/check-runs"), do: ok(%{"check_runs" => s.checks}, s), else: ok([], s)
  end

  defp respond("POST", "/graphql", _, body, s) do
    if body["variables"]["issue"] do
      ok(%{"data" => %{"repository" => %{"issue" => %{"closedByPullRequestsReferences" => %{"nodes" => s.linked, "pageInfo" => %{"hasNextPage" => false}}}}}}, s)
    else
      gate = Map.merge(s.gate, %{"headRefOid" => s.sha, "baseRefName" => s.base})
      ok(%{"data" => %{"repository" => %{"pullRequest" => gate}}}, s)
    end
  end

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
