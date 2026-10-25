defmodule SymphonyElixir.McpBridge do
  @moduledoc """
  Loopback MCP endpoint that exposes the tracker's agent tools to runtimes that
  cannot register custom tools natively (pi, and later opencode).

  Transport is Streamable HTTP: one POST per JSON-RPC message, JSON response.
  The bearer token is generated per service start and only ever written into the
  workspace `.pi/mcp.json`, so it never enters the agent's environment.
  """

  use GenServer
  require Logger

  alias SymphonyElixir.Tracker

  @mcp_path "/mcp"

  defmodule Router do
    @behaviour Plug
    import Plug.Conn

    alias SymphonyElixir.Tracker

    @mcp_path "/mcp"
    @protocol_version "2025-06-18"
    @server_version "0.0.3"

    @impl true
    def init(opts), do: opts

    @impl true
    def call(conn, opts) do
      token = Keyword.fetch!(opts, :token)

      cond do
        conn.method != "POST" or conn.request_path != @mcp_path ->
          conn |> send_resp(404, "") |> halt()

        not authorized?(conn, token) ->
          conn
          |> put_resp_content_type("application/json")
          |> send_resp(401, Jason.encode!(%{"error" => "unauthorized"}))
          |> halt()

        true ->
          handle(conn)
      end
    end

    defp authorized?(conn, token) when is_binary(token) do
      case get_req_header(conn, "authorization") do
        ["Bearer " <> candidate] -> Plug.Crypto.secure_compare(candidate, token)
        _ -> false
      end
    end

    defp handle(conn) do
      {:ok, body, conn} = read_body(conn)

      case Jason.decode(body) do
        {:ok, %{"method" => method} = request} -> dispatch(conn, method, request)
        {:ok, _other} -> rpc_error(conn, nil, -32600, "Invalid Request")
        {:error, _reason} -> rpc_error(conn, nil, -32700, "Parse error")
      end
    end

    defp dispatch(conn, "initialize", request) do
      rpc_result(conn, request, %{
        "protocolVersion" => @protocol_version,
        "capabilities" => %{"tools" => %{}},
        "serverInfo" => %{"name" => "symphony", "version" => @server_version}
      })
    end

    defp dispatch(conn, "notifications/initialized", _request) do
      conn |> send_resp(202, "") |> halt()
    end

    defp dispatch(conn, "ping", request) do
      rpc_result(conn, request, %{})
    end

    defp dispatch(conn, "tools/list", request) do
      tools =
        Tracker.bind_agent_tools()
        |> Map.get(:tool_specs, [])
        |> Enum.map(&normalise_tool_spec/1)

      rpc_result(conn, request, %{"tools" => tools})
    end

    defp dispatch(conn, "tools/call", request) do
      params = Map.get(request, "params") || %{}
      name = Map.get(params, "name")
      arguments = Map.get(params, "arguments") || %{}

      if is_binary(name) and name != "" do
        binding = Tracker.bind_agent_tools()
        result = Tracker.execute_bound_agent_tool(binding, name, arguments, [])
        rpc_result(conn, request, to_mcp_result(result))
      else
        rpc_error(conn, request, -32602, "Missing tool name")
      end
    end

    defp dispatch(conn, _method, request) do
      rpc_error(conn, request, -32601, "Method not found")
    end

    defp normalise_tool_spec(spec) do
      %{
        "name" => Map.get(spec, "name"),
        "description" => Map.get(spec, "description", ""),
        "inputSchema" => Map.get(spec, "inputSchema") || Map.get(spec, "input_schema") || %{"type" => "object"}
      }
    end

    defp to_mcp_result(%{"success" => success, "output" => output}) when is_binary(output) do
      %{
        "content" => [%{"type" => "text", "text" => output}],
        "isError" => success != true
      }
    end

    defp to_mcp_result(other) do
      text =
        case Jason.encode(other, pretty: true) do
          {:ok, encoded} -> encoded
          _ -> inspect(other)
        end

      %{"content" => [%{"type" => "text", "text" => text}], "isError" => false}
    end

    defp rpc_result(conn, request, result) do
      body = %{"jsonrpc" => "2.0", "id" => Map.get(request, "id"), "result" => result}

      conn
      |> put_resp_content_type("application/json")
      |> send_resp(200, Jason.encode!(body))
      |> halt()
    end

    defp rpc_error(conn, request, code, message) do
      body = %{
        "jsonrpc" => "2.0",
        "id" => if(is_map(request), do: Map.get(request, "id"), else: nil),
        "error" => %{"code" => code, "message" => message}
      }

      conn
      |> put_resp_content_type("application/json")
      |> send_resp(200, Jason.encode!(body))
      |> halt()
    end
  end

  @spec start_link(keyword()) :: GenServer.on_start()
  def start_link(opts \\ []) do
    GenServer.start_link(__MODULE__, opts, name: __MODULE__)
  end

  @spec child_spec(keyword()) :: Supervisor.child_spec()
  def child_spec(opts) do
    %{id: __MODULE__, start: {__MODULE__, :start_link, [opts]}, type: :worker}
  end

  @doc "MCP client configuration for a runtime workspace, or `:disabled`."
  @spec client_config() :: {:ok, map()} | :disabled
  def client_config do
    case Process.whereis(__MODULE__) do
      nil -> :disabled
      pid -> GenServer.call(pid, :client_config, 5_000)
    end
  catch
    :exit, _reason -> :disabled
  end

  @doc "Endpoint URL and bearer token; used by tests and diagnostics."
  @spec endpoint() :: {:ok, String.t(), String.t()} | :disabled
  def endpoint do
    case Process.whereis(__MODULE__) do
      nil -> :disabled
      pid -> GenServer.call(pid, :endpoint, 5_000)
    end
  catch
    :exit, _reason -> :disabled
  end

  @impl true
  def init(_opts) do
    start_bridge()
  end

  defp start_bridge do
    token = Base.url_encode64(:crypto.strong_rand_bytes(32), padding: false)
    port = free_port()

    {:ok, bandit} =
      Bandit.start_link(
        plug: {Router, token: token},
        ip: {127, 0, 0, 1},
        port: port,
        startup_log: false
      )

    Logger.info("MCP bridge listening on 127.0.0.1:#{port}#{@mcp_path}")

    {:ok, %{pid: bandit, port: port, token: token}}
  end

  defp enabled? do
    case SymphonyElixir.Config.settings() do
      {:ok, settings} ->
        settings.runtime.kind == "pi" and Map.get(settings.runtime.mcp, "enabled") == true

      _ ->
        false
    end
  end

  @impl true
  def handle_call(:client_config, _from, state) do
    if enabled?() do
      {:reply, {:ok, client_config_map(state)}, state}
    else
      {:reply, :disabled, state}
    end
  end

  def handle_call(:endpoint, _from, state) do
    {:reply, {:ok, "http://127.0.0.1:#{state.port}#{@mcp_path}", state.token}, state}
  end

  defp client_config_map(state) do
    %{
      "mcpServers" => %{
        "symphony" => %{
          "url" => "http://127.0.0.1:#{state.port}#{@mcp_path}",
          "headers" => %{"Authorization" => "Bearer #{state.token}"},
          "exposure" => "direct",
          "description" => "Symphony tracker tools (issues, comments, pull requests)."
        }
      }
    }
  end

  defp free_port do
    {:ok, socket} = :gen_tcp.listen(0, [:binary, ip: {127, 0, 0, 1}, active: false])
    {:ok, port} = :inet.port(socket)
    :gen_tcp.close(socket)
    port
  end
end
