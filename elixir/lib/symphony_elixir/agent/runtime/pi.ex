defmodule SymphonyElixir.Agent.Runtime.Pi do
  @moduledoc """
  Pi runtime: drives `pi --mode rpc` over strict JSONL on stdin/stdout.

  Pi is a long-lived subprocess; one session serves every turn of a single
  agent run. Only `agent_settled` ends a turn — `agent_end` may still be
  followed by retries, compaction or queued follow-ups.

  Events are normalised into the message shape `SymphonyElixir.AgentRunner`
  already forwards to the orchestrator: `:event`, `:timestamp`, and a `payload`
  that carries usage as `method: "turn/completed"` + an integer usage map.
  """

  @behaviour SymphonyElixir.Agent.Runtime

  require Logger

  alias SymphonyElixir.Config

  @abort_grace_ms 5_000

  @impl true
  def start_session(workspace, opts) do
    config = Config.settings!().runtime

    cond do
      Keyword.get(opts, :worker_host) ->
        {:error, :pi_remote_worker_not_supported}

      not is_binary(config.command) or String.trim(config.command) == "" ->
        {:error, :missing_pi_command}

      true ->
        with :ok <- sanitise_pi_dir(workspace),
             :ok <- write_mcp_config(workspace),
             :ok <- exclude_pi_dir(workspace),
             {:ok, session} <- open_port(workspace, config) do
          {:ok, open_live_log(session, workspace, config)}
        end
    end
  end

  @impl true
  def run_turn(session, prompt, _issue, opts) do
    on_message = Keyword.get(opts, :on_message, fn _ -> :ok end)
    request_id = "symphony-#{System.unique_integer([:positive])}"
    session_id = session.session_id

    emit(on_message, :session_started, %{
      session_id: session_id,
      payload: %{"method" => "session/started", "session_id" => session_id}
    })

    with :ok <- send_command(session.port, %{"id" => request_id, "type" => "prompt", "message" => prompt}) do
      state = %{session | buffer: "", usage: empty_usage(), session_id: session_id}
      deadline = System.monotonic_time(:millisecond) + session.config.turn_timeout_ms

      case await_turn(state, request_id, on_message, deadline) do
        {:ok, state, :settled} ->
          usage = finalise_usage(state.usage)

          emit(on_message, :turn_completed, %{
            session_id: session_id,
            payload: %{"method" => "turn/completed", "usage" => usage},
            usage: usage
          })

          {:ok, %{session_id: session_id, turn_id: request_id, usage: usage}}

        {:ok, state, :handled} ->
          usage = finalise_usage(state.usage)
          {:ok, %{session_id: session_id, turn_id: request_id, usage: usage}}

        {:ok, _state, :aborted} ->
          emit(on_message, :turn_cancelled, %{
            session_id: session_id,
            payload: %{"method" => "turn/cancelled"}
          })

          {:error, :turn_aborted}

        {:error, reason} ->
          emit(on_message, :turn_failed, %{
            session_id: session_id,
            payload: %{"method" => "turn/failed", "error" => inspect(reason)},
            reason: inspect(reason)
          })

          {:error, reason}
      end
    end
  end

  @impl true
  def stop_session(%{port: port}) do
    os_pid =
      case Port.info(port, :os_pid) do
        {:os_pid, pid} when is_integer(pid) -> pid
        _ -> nil
      end

    close_port(port)

    if is_integer(os_pid) do
      System.cmd("kill", ["-TERM", Integer.to_string(os_pid)], stderr_to_stdout: true)
    end

    :ok
  end

  @impl true
  def capabilities do
    %{tools: :mcp, approvals: false, sandbox: false, interrupt: true, usage: :message}
  end

  @impl true
  def name, do: "pi"

  defp open_port(workspace, config) do
    bash =
      System.find_executable("bash") ||
        raise RuntimeError, "pi runtime requires bash on PATH"

    session_id = new_session_id()
    script = build_script(config, Config.settings!().tracker.secret_environment_names, session_id)

    Logger.info("Starting pi runtime workspace=#{workspace} session=#{session_id} command=#{config.command}")

    port =
      Port.open({:spawn_executable, bash}, [
        :binary,
        :exit_status,
        args: ["-lc", script],
        cd: workspace
      ])

    {:ok,
     %{
       port: port,
       workspace: workspace,
       config: config,
       buffer: "",
       usage: empty_usage(),
       session_id: session_id
     }}
  rescue
    error -> {:error, {:pi_start_failed, Exception.message(error)}}
  end

  defp new_session_id do
    <<a::32, b::16, c0::16, d0::16, e::48>> = :crypto.strong_rand_bytes(16)
    c = Bitwise.bor(Bitwise.band(c0, 0x0FFF), 0x4000)
    d = Bitwise.bor(Bitwise.band(d0, 0x3FFF), 0x8000)

    [
      Base.encode16(<<a::32>>, case: :lower),
      Base.encode16(<<b::16>>, case: :lower),
      Base.encode16(<<c::16>>, case: :lower),
      Base.encode16(<<d::16>>, case: :lower),
      Base.encode16(<<e::48>>, case: :lower)
    ]
    |> Enum.join("-")
  end

  defp build_script(config, secret_names, session_id) do
    unsets =
      secret_names
      |> Enum.map_join(" ", &"unset #{&1};")

    command =
      [config.command, session_id_flag(config, session_id), model_flag(config), thinking_flag(config)]
      |> Enum.reject(&(&1 in [nil, ""]))
      |> Enum.join(" ")

    [unsets, "exec", command]
    |> Enum.reject(&(&1 == ""))
    |> Enum.join(" ")
  end

  defp session_id_flag(%{command: command}, session_id) do
    if is_binary(command) and Regex.match?(~r/--session[\s=]|--session-id[\s=]/, command) do
      nil
    else
      "--session-id #{shell_quote(session_id)}"
    end
  end

  defp model_flag(%{model: model}) when is_binary(model) and model != "", do: "--model #{shell_quote(model)}"
  defp model_flag(_config), do: nil

  defp thinking_flag(%{thinking: thinking}) when is_binary(thinking) and thinking != "",
    do: "--thinking #{shell_quote(thinking)}"

  defp thinking_flag(_config), do: nil

  defp shell_quote(value) do
    "'" <> String.replace(value, "'", "'\\''") <> "'"
  end

  # `--approve` trusts project resources, so anything the agent left behind in
  # `.pi` would load in the next run. Keep only the MCP config this module writes.
  @pi_project_entries ~w(extensions skills prompts themes SYSTEM.md APPEND_SYSTEM.md settings.json)

  defp sanitise_pi_dir(workspace) do
    dir = Path.join(workspace, ".pi")
    Enum.each(@pi_project_entries, &File.rm_rf(Path.join(dir, &1)))
    :ok
  end

  # `.pi/mcp.json` carries the bridge bearer token; keep it out of any commit
  # the agent makes with `git add -A`.
  defp exclude_pi_dir(workspace) do
    exclude = Path.join([workspace, ".git", "info", "exclude"])
    entry = ".pi/"

    with true <- File.dir?(Path.dirname(exclude)),
         {:ok, content} <- File.read(exclude),
         false <- String.contains?(content, entry) do
      File.write(exclude, String.trim_trailing(content) <> "\n" <> entry <> "\n")
    else
      _ -> :ok
    end
  end

  defp write_mcp_config(workspace) do
    case SymphonyElixir.McpBridge.client_config() do
      :disabled ->
        :ok

      {:ok, mcp_config} ->
        dir = Path.join(workspace, ".pi")
        path = Path.join(dir, "mcp.json")

        with :ok <- File.mkdir_p(dir),
             :ok <- File.write(path, Jason.encode!(mcp_config, pretty: true)) do
          :ok
        else
          error -> {:error, {:pi_mcp_config_failed, error}}
        end
    end
  end

  defp send_command(port, record) do
    payload = [Jason.encode!(record), "\n"]

    case Port.command(port, payload) do
      true -> :ok
      false -> {:error, :pi_write_failed}
    end
  rescue
    error -> {:error, {:pi_write_failed, Exception.message(error)}}
  end

  defp await_turn(state, request_id, on_message, deadline) do
    timeout = max(deadline - System.monotonic_time(:millisecond), 0)

    receive do
      {port, {:data, data}} when port == state.port ->
        {records, state} = decode(state, data)

        case reduce(records, state, request_id, on_message) do
          {:continue, state} -> await_turn(state, request_id, on_message, deadline)
          {:done, state, outcome} -> {:ok, state, outcome}
          {:error, reason} -> {:error, reason}
        end

      {port, {:exit_status, code}} when port == state.port ->
        {:error, {:pi_exited, code}}
    after
      timeout ->
        abort_turn(state.port, request_id, on_message, state.session_id)
    end
  end

  defp reduce(records, state, request_id, on_message) do
    Enum.reduce_while(records, {:continue, state}, fn record, {:continue, state} ->
      case handle_record(record, state, request_id, on_message) do
        {:continue, state} -> {:cont, {:continue, state}}
        other -> {:halt, other}
      end
    end)
  end

  defp handle_record(%{"type" => "response", "id" => id, "success" => false} = record, _state, id, _on_message) do
    {:error, {:pi_command_failed, Map.get(record, "error", "unknown")}}
  end

  defp handle_record(%{"type" => "response", "id" => id, "success" => true} = record, state, id, _on_message) do
    case get_in(record, ["data", "disposition"]) do
      "handled" -> {:done, state, :handled}
      _ -> {:continue, state}
    end
  end

  defp handle_record(%{"type" => "agent_settled"} = record, state, _request_id, _on_message) do
    {:done, state, if(Map.get(record, "aborted") == true, do: :aborted, else: :settled)}
  end

  defp handle_record(%{"type" => "message_end", "message" => %{"role" => "assistant"} = message}, state, _id, on_message) do
    usage = accumulate_usage(state.usage, Map.get(message, "usage"))

    # A run can be stopped by the host the moment the issue leaves the routing
    # set (pi clears `agent-ready` on completion), which kills the process before
    # `agent_settled` arrives. Report usage per assistant message so totals are
    # already accumulated; the orchestrator's monotonic delta handles repeats.
    emit(on_message, :notification, %{
      session_id: state.session_id,
      payload: %{"method" => "turn/completed", "usage" => finalise_usage(usage)},
      usage: finalise_usage(usage)
    })

    {:continue, %{state | usage: usage}}
  end

  defp handle_record(%{"type" => "tool_execution_end"} = record, state, _id, on_message) do
    event = if Map.get(record, "isError") == true, do: :tool_call_failed, else: :tool_call_completed

    emit(on_message, event, %{
      session_id: state.session_id,
      details: %{
        "tool" => Map.get(record, "toolName"),
        "duration_ms" => Map.get(record, "durationMs"),
        "error" => Map.get(record, "isError") == true
      },
      payload: %{
        "method" => "item/tool/call",
        "params" => %{
          "tool" => Map.get(record, "toolName"),
          "isError" => Map.get(record, "isError")
        }
      }
    })

    {:continue, state}
  end

  defp handle_record(%{"type" => "error"} = record, _state, _id, _on_message) do
    {:error, {:pi_error, Map.get(record, "message", "unknown")}}
  end

  defp handle_record(_record, state, _id, _on_message), do: {:continue, state}

  defp abort_turn(port, request_id, on_message, session_id) do
    _ = send_command(port, %{"id" => "#{request_id}-abort", "type" => "abort"})
    deadline = System.monotonic_time(:millisecond) + @abort_grace_ms

    case await_settle(port, on_message, session_id, deadline) do
      :ok -> {:error, :turn_timeout}
      :timeout -> {:error, :turn_timeout}
      :exited -> {:error, :turn_timeout}
    end
  end

  defp await_settle(port, _on_message, _session_id, deadline) do
    timeout = max(deadline - System.monotonic_time(:millisecond), 0)

    receive do
      {^port, {:data, data}} ->
        if String.contains?(data, "\"agent_settled\"") do
          :ok
        else
          await_settle(port, nil, nil, deadline)
        end

      {^port, {:exit_status, _code}} ->
        :exited
    after
      timeout -> :timeout
    end
  end

  defp open_live_log(session, workspace, config) do
    case live_log_dir(config) do
      nil ->
        session

      dir ->
        path =
          Path.join(dir, "#{live_timestamp()}_#{sanitize_name(session.session_id)}.live.jsonl")

        with :ok <- File.mkdir_p(dir),
             :ok <- File.write(path, live_header(session.session_id, workspace)) do
          Logger.info("Mirroring pi rpc stream workspace=#{workspace} live_log=#{path}")
          Map.put(session, :live_path, path)
        else
          reason ->
            Logger.warning("Live session mirror disabled workspace=#{workspace} reason=#{inspect(reason)}")

            session
        end
    end
  end

  defp live_log_dir(%{live_log_dir: dir}) when is_binary(dir) and dir != "", do: dir
  defp live_log_dir(_config), do: nil

  defp live_timestamp do
    DateTime.utc_now()
    |> DateTime.truncate(:millisecond)
    |> DateTime.to_iso8601()
    |> String.replace(":", "-")
  end

  defp sanitize_name(name) when is_binary(name), do: String.replace(name, ~r/[^A-Za-z0-9._-]/, "_")
  defp sanitize_name(_name), do: "session"

  defp live_header(session_id, workspace) do
    Jason.encode!(%{
      "type" => "session",
      "version" => 3,
      "id" => session_id,
      "timestamp" => DateTime.utc_now() |> DateTime.truncate(:millisecond) |> DateTime.to_iso8601(),
      "cwd" => workspace
    }) <> "\n"
  end

  defp mirror_lines(nil, _lines), do: :ok
  defp mirror_lines(_path, []), do: :ok

  defp mirror_lines(path, lines) do
    File.write(path, Enum.map_join(lines, "", &(&1 <> "\n")), [:append])
    :ok
  rescue
    _ -> :ok
  end

  defp decode(state, data) do
    parts = String.split(state.buffer <> data, "\n")
    {complete, [rest]} = Enum.split(parts, -1)
    mirror_lines(Map.get(state, :live_path), complete)

    records =
      complete
      |> Enum.map(&String.trim_trailing(&1, "\r"))
      |> Enum.reject(&(&1 == ""))
      |> Enum.flat_map(&parse_record/1)

    {records, %{state | buffer: rest}}
  end

  defp parse_record(line) do
    case Jason.decode(line) do
      {:ok, %{} = record} -> [record]
      _ -> []
    end
  end

  defp empty_usage, do: %{input: 0, output: 0}

  defp accumulate_usage(usage, nil), do: usage

  defp accumulate_usage(usage, message_usage) when is_map(message_usage) do
    input = integer_or_zero(Map.get(message_usage, "input"))
    output = integer_or_zero(Map.get(message_usage, "output"))

    %{input: max(usage.input, input), output: usage.output + output}
  end

  defp finalise_usage(usage) do
    %{
      "input_tokens" => usage.input,
      "output_tokens" => usage.output,
      "total_tokens" => usage.input + usage.output
    }
  end

  defp integer_or_zero(value) when is_integer(value) and value >= 0, do: value
  defp integer_or_zero(_value), do: 0

  defp emit(on_message, event, fields) do
    on_message.(Map.merge(%{event: event, timestamp: DateTime.utc_now()}, fields))
  end

  defp close_port(port) do
    Port.close(port)
  catch
    _, _ -> :ok
  end
end
