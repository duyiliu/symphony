defmodule SymphonyElixir.Agent.Runtime.PiTest do
  use SymphonyElixir.TestSupport

  alias SymphonyElixir.Agent.Runtime.Pi

  setup do
    workspace = Path.join(System.tmp_dir!(), "pi-runtime-#{System.unique_integer([:positive])}")
    File.mkdir_p!(workspace)
    on_exit(fn -> File.rm_rf(workspace) end)

    {:ok, workspace: workspace}
  end

  test "completes a turn and reports normalised usage", %{workspace: workspace} do
    command =
      fake_pi!(
        workspace,
        ok_response() <>
          """
          printf '%s\\n' '{"type":"message_end","message":{"role":"assistant","content":[{"type":"text","text":"done"}],"usage":{"input":10,"output":2,"totalTokens":12}}}'
          printf '%s\\n' '{"type":"agent_settled","aborted":false}'
          """
      )

    configure_pi!(command)

    {:ok, session} = Pi.start_session(workspace, [])
    assert {:ok, result} = Pi.run_turn(session, "hi", issue(), on_message: notify())
    assert result.usage == %{"input_tokens" => 10, "output_tokens" => 2, "total_tokens" => 12}

    assert_received {:agent_message, %{event: :turn_completed} = message}

    assert message.payload == %{
             "method" => "turn/completed",
             "usage" => %{"input_tokens" => 10, "output_tokens" => 2, "total_tokens" => 12}
           }

    Pi.stop_session(session)
  end

  test "surfaces tool execution results as tool events", %{workspace: workspace} do
    command =
      fake_pi!(
        workspace,
        ok_response() <>
          """
          printf '%s\\n' '{"type":"tool_execution_end","toolCallId":"c1","toolName":"bash","result":{"content":[]},"isError":false,"durationMs":12}'
          printf '%s\\n' '{"type":"agent_settled","aborted":false}'
          """
      )

    configure_pi!(command)

    {:ok, session} = Pi.start_session(workspace, [])
    assert {:ok, _result} = Pi.run_turn(session, "hi", issue(), on_message: notify())

    assert_received {:agent_message, %{event: :tool_call_completed} = message}
    assert message.details["tool"] == "bash"

    Pi.stop_session(session)
  end

  test "fails the turn when a command is rejected", %{workspace: workspace} do
    command =
      fake_pi!(workspace, """
      IFS= read -r line
      id=$(printf '%s' "$line" | sed -n 's/.*"id":"\\([^"]*\\)".*/\\1/p')
      printf '{"id":"%s","type":"response","command":"prompt","success":false,"error":"model not found"}\\n' "$id"
      """)

    configure_pi!(command)

    {:ok, session} = Pi.start_session(workspace, [])

    assert {:error, {:pi_command_failed, "model not found"}} =
             Pi.run_turn(session, "hi", issue(), on_message: notify())

    Pi.stop_session(session)
  end

  test "reports an aborted turn as cancelled", %{workspace: workspace} do
    command =
      fake_pi!(
        workspace,
        ok_response() <>
          """
          printf '%s\\n' '{"type":"agent_settled","aborted":true}'
          """
      )

    configure_pi!(command)

    {:ok, session} = Pi.start_session(workspace, [])
    assert {:error, :turn_aborted} = Pi.run_turn(session, "hi", issue(), on_message: notify())
    assert_received {:agent_message, %{event: :turn_cancelled}}

    Pi.stop_session(session)
  end

  test "fails when the pi process exits mid-turn", %{workspace: workspace} do
    command =
      fake_pi!(workspace, """
      IFS= read -r line
      exit 3
      """)

    configure_pi!(command)

    {:ok, session} = Pi.start_session(workspace, [])
    assert {:error, {:pi_exited, 3}} = Pi.run_turn(session, "hi", issue(), on_message: notify())

    Pi.stop_session(session)
  end

  test "aborts and times out a stalled turn", %{workspace: workspace} do
    command =
      fake_pi!(workspace, """
      IFS= read -r line
      sleep 30
      """)

    configure_pi!(command, turn_timeout_ms: 200)

    {:ok, session} = Pi.start_session(workspace, [])
    assert {:error, :turn_timeout} = Pi.run_turn(session, "hi", issue(), on_message: notify())

    Pi.stop_session(session)
  end

  defp ok_response do
    """
    IFS= read -r line
    id=$(printf '%s' "$line" | sed -n 's/.*"id":"\\([^"]*\\)".*/\\1/p')
    printf '{"id":"%s","type":"response","command":"prompt","success":true,"data":{"disposition":"started"}}\\n' "$id"
    """
  end

  defp notify do
    test_pid = self()
    fn message -> send(test_pid, {:agent_message, message}) end
  end

  defp configure_pi!(command, opts \\ []) do
    write_workflow_file!(Workflow.workflow_file_path(),
      runtime_kind: "pi",
      runtime_command: command,
      runtime_turn_timeout_ms: Keyword.get(opts, :turn_timeout_ms, 30_000)
    )

    WorkflowStore.force_reload()
  end

  defp fake_pi!(dir, body) do
    path = Path.join(dir, "fake-pi.sh")
    File.write!(path, "#!/bin/sh\n" <> body <> "\n")
    File.chmod!(path, 0o755)
    path
  end

  defp issue do
    %SymphonyElixir.Tracker.Issue{
      id: "1",
      identifier: "TEST-1",
      title: "test",
      description: "",
      state: "open"
    }
  end
end
