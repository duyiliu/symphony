defmodule SymphonyElixir.GitHub.WorkflowIntegrationTest do
  use SymphonyElixir.TestSupport

  alias SymphonyElixir.GitHub.WorkflowPolicy

  setup do
    workflow_path = Workflow.workflow_file_path()
    state_dir = Path.join(Path.dirname(workflow_path), "private/receipts")

    sample =
      Path.expand("../../WORKFLOW.github.md", __DIR__)
      |> File.read!()
      |> String.replace("$SYMPHONY_GITHUB_TOKEN", "offline-test-token")
      |> String.replace("/var/lib/symphony-github/receipts", state_dir)

    File.write!(workflow_path, sample)
    assert :ok = WorkflowStore.force_reload()
    :ok
  end

  test "sample configuration parses, tools bind policy and GitHub API cannot bypass it" do
    assert Config.settings!().tracker.github_workflow["enabled"] == true
    binding = Tracker.bind_agent_tools()
    assert Enum.map(binding.tool_specs, & &1["name"]) == ["github_api", "github_workflow"]

    result =
      Tracker.execute_bound_agent_tool(
        binding,
        "github_api",
        %{
          "method" => "PUT",
          "path" => "/repos/OWNER/REPO/pulls/2/merge",
          "body" => %{}
        },
        github_client: fn _, _, _, _, _ -> flunk("Denied mutation reached GitHub") end
      )

    refute result["success"]
    assert result["output"] =~ "github_workflow_policy_denied"
    assert PromptBuilder.build_prompt(issue(["agent"])) =~ "Host-enforced execution contract"
    assert PromptBuilder.build_prompt(issue(["agent"])) =~ "Auto merge"
    assert PromptBuilder.build_prompt(issue(["agent"])) =~ "needs-human"
  end

  test "dispatch refresh runs only on agent and refuses conflicting labels" do
    original = issue(["agent"])

    assert {:ok, ^original} =
             Orchestrator.revalidate_issue_for_dispatch_for_test(original, fn _ -> {:ok, [issue(["agent"])]} end)

    # needs-human pauses, agent conflicts fail closed and legacy labels alone are inert.
    skipped = [["merge-approved"], ["needs-human"], ["agent", "needs-human"], []]

    for labels <- skipped do
      fetch = fn _ -> {:ok, [issue(labels)]} end
      assert {:skip, _} = Orchestrator.revalidate_issue_for_dispatch_for_test(original, fetch)
    end

    # A leftover legacy label does not block execution.
    legacy = issue(["agent", "merge-approved"])
    assert {:ok, ^legacy} = Orchestrator.revalidate_issue_for_dispatch_for_test(original, fn _ -> {:ok, [legacy]} end)
  end

  test "execution label semantics keep phases separate across continuations" do
    develop = issue(["agent"])
    risky = issue(["agent", "high-risk"])
    assert {:continue, ^risky} = AgentRunner.continue_with_issue_for_test(develop, fn _ -> {:ok, [risky]} end)
    assert {:done, _} = AgentRunner.continue_with_issue_for_test(develop, fn _ -> {:ok, [issue(["needs-human"])]} end)
    assert {:done, _} = AgentRunner.continue_with_issue_for_test(develop, fn _ -> {:ok, [issue(["agent", "needs-human"])]} end)
  end

  test "state storage cannot be inside workspaces and default Linear behavior remains unchanged" do
    settings = Config.settings!()
    bad = put_in(settings.tracker.github_workflow["state_dir"], Path.join(settings.workspace.root, "receipts"))
    assert {:error, :github_workflow_store_overlaps_workspaces} = Config.validate_settings(bad)
    assert :ok = WorkflowPolicy.validate(%{settings.tracker | github_workflow: %{}})
    linear = %{settings.tracker | kind: "linear", required_labels: []}
    assert WorkflowPolicy.routable?(issue([]), linear)
  end

  test "paused worker exits into blocked state without a continuation timer" do
    settings = Config.settings!().tracker
    dir = settings.github_workflow["state_dir"]
    File.mkdir_p!(dir)
    File.write!(Path.join(dir, "1.json"), Jason.encode!(%{"repo" => "OWNER/REPO", "cycle" => "develop:1", "attempts" => 1, "blocked" => true}))

    client = fn _method, path, _params, _body, _options ->
      body =
        if String.ends_with?(path, "/events"),
          do: [%{"id" => 1, "event" => "labeled", "label" => %{"name" => "agent"}}],
          else: %{"state" => "open", "labels" => [%{"name" => "agent"}]}

      {:ok, %{status: 200, body: body}}
    end

    task_issue = issue(["agent"])
    {pid, ref} = spawn_monitor(fn -> AgentRunner.run(task_issue, nil, github_client: client) end)
    assert_receive {:DOWN, ^ref, :process, ^pid, {:shutdown, :github_workflow_waiting}}, 2_000
    entry = %{pid: pid, ref: ref, identifier: "GH-1", issue: task_issue, started_at: DateTime.utc_now()}
    state = %Orchestrator.State{running: %{"1" => entry}, claimed: MapSet.new(["1"]), codex_totals: %{}}
    down = {:DOWN, ref, :process, pid, {:shutdown, :github_workflow_waiting}}
    assert {:noreply, blocked} = Orchestrator.handle_info(down, state)
    assert blocked.blocked["1"].github_workflow_waiting
    assert blocked.retry_attempts == %{}
    assert blocked.running == %{}
    refute MapSet.member?(blocked.completed, "1")
  end

  defp issue(labels) do
    %Issue{id: "1", identifier: "GH-1", title: "Task", state: "open", labels: labels, dispatchable: true}
  end
end
