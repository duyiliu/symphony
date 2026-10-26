defmodule SymphonyElixir.GitHub.WorkflowIntegrationTest do
  use SymphonyElixir.TestSupport

  alias SymphonyElixir.GitHub.WorkflowPolicy

  setup do
    workflow_path = Workflow.workflow_file_path()
    state_dir = Path.join(Path.dirname(workflow_path), "private/receipts")

    sample =
      Path.expand("../../WORKFLOW.github.md", __DIR__)
      |> File.read!()
      |> String.replace("isolated_agent: false", "isolated_agent: true")
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
  end

  test "dispatch refresh uses both labels and refuses their intersection" do
    original = issue(["agent"])
    merge = issue(["merge-approved"])
    invalid = issue(["agent", "merge-approved"])
    assert {:ok, ^merge} = Orchestrator.revalidate_issue_for_dispatch_for_test(original, fn _ -> {:ok, [merge]} end)
    fetch_invalid = fn _ -> {:ok, [invalid]} end
    assert {:skip, ^invalid} = Orchestrator.revalidate_issue_for_dispatch_for_test(original, fetch_invalid)
    assert {:skip, _} = Orchestrator.revalidate_issue_for_dispatch_for_test(original, fn _ -> {:ok, [issue([])]} end)
  end

  test "continuation does not reuse development context for a merge phase" do
    develop = issue(["agent"])
    merge = issue(["merge-approved"])
    assert {:done, ^merge} = AgentRunner.continue_with_issue_for_test(develop, fn _ -> {:ok, [merge]} end)
    assert {:done, ^develop} = AgentRunner.continue_with_issue_for_test(merge, fn _ -> {:ok, [develop]} end)
    assert {:continue, ^develop} = AgentRunner.continue_with_issue_for_test(develop, fn _ -> {:ok, [develop]} end)
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
