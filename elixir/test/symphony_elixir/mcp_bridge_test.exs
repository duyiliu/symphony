defmodule SymphonyElixir.McpBridgeTest do
  use SymphonyElixir.TestSupport

  alias SymphonyElixir.McpBridge
  alias SymphonyElixir.McpBridge.Router

  @token "test-token"

  setup do
    if Process.whereis(McpBridge) == nil, do: start_supervised!(McpBridge)
    :ok
  end

  describe "MCP router" do
    test "initialize negotiates the protocol version and server identity" do
      body = rpc_call("initialize", %{"protocolVersion" => "2025-06-18"})

      assert body["jsonrpc"] == "2.0"
      assert body["id"] == 1

      assert body["result"] == %{
               "protocolVersion" => "2025-06-18",
               "capabilities" => %{"tools" => %{}},
               "serverInfo" => %{"name" => "symphony", "version" => "0.0.3"}
             }
    end

    test "notifications/initialized is acknowledged without a json-rpc body" do
      conn = post("notifications/initialized", nil) |> authorize() |> call()

      assert conn.status == 202
      assert conn.resp_body == ""
    end

    test "ping answers with an empty result" do
      assert %{"result" => %{}} = rpc_call("ping")
    end

    test "tools/list advertises the tracker tools with MCP input schemas" do
      body = rpc_call("tools/list")

      assert [tool] = body["result"]["tools"]
      assert tool["name"] == "linear_graphql"
      assert is_binary(tool["description"])
      assert tool["inputSchema"]["type"] == "object"
      assert tool["inputSchema"]["required"] == ["query"]
    end

    test "tools/call reports an unsupported tool as an error result" do
      body = rpc_call("tools/call", %{"name" => "not_a_real_tool", "arguments" => %{}})

      assert [%{"type" => "text", "text" => text}] = body["result"]["content"]
      assert body["result"]["isError"] == true
      assert text =~ "Unsupported dynamic tool"
    end

    test "tools/call without a tool name is an invalid params error" do
      body = rpc_call("tools/call", %{"arguments" => %{}})

      assert body["error"]["code"] == -32_602
    end

    test "unknown methods are rejected" do
      assert %{"error" => %{"code" => -32_601}} = rpc_call("resources/list")
    end

    test "malformed json is a parse error" do
      conn =
        Plug.Test.conn(:post, "/mcp", "{not json")
        |> authorize()
        |> call()

      assert Jason.decode!(conn.resp_body)["error"]["code"] == -32_700
    end

    test "a request without the bearer token is unauthorized" do
      conn = post("ping", nil) |> call()

      assert conn.status == 401
      assert Jason.decode!(conn.resp_body) == %{"error" => "unauthorized"}
    end

    test "a request with a wrong bearer token is unauthorized" do
      conn =
        post("ping", nil)
        |> Plug.Conn.put_req_header("authorization", "Bearer nope")
        |> call()

      assert conn.status == 401
    end

    test "other paths and methods are not found" do
      assert call(Plug.Test.conn(:post, "/other", rpc_body("ping"))).status == 404
      assert call(Plug.Test.conn(:get, "/mcp", "")).status == 404
    end
  end

  describe "client configuration" do
    test "stays disabled unless the pi runtime opts in" do
      assert McpBridge.client_config() == :disabled
    end

    test "points an opted-in pi runtime at the loopback endpoint" do
      write_workflow_file!(Workflow.workflow_file_path(),
        runtime_kind: "pi",
        runtime_command: "pi --mode rpc",
        runtime_mcp: %{"enabled" => true}
      )

      assert {:ok, %{"mcpServers" => %{"symphony" => server}}} = McpBridge.client_config()
      assert server["url"] =~ ~r{^http://127\.0\.0\.1:\d+/mcp$}
      assert server["headers"]["Authorization"] =~ ~r{^Bearer .+$}
      assert server["exposure"] == "direct"
    end
  end

  describe "loopback transport" do
    test "rejects a request without the bearer token" do
      {:ok, url, _token} = McpBridge.endpoint()

      assert {401, body} = http_post(url, [], rpc_body("ping"))
      assert Jason.decode!(body) == %{"error" => "unauthorized"}
    end

    test "answers an authenticated request over bandit" do
      {:ok, url, token} = McpBridge.endpoint()
      headers = [{"authorization", "Bearer " <> token}]

      assert {200, body} = http_post(url, headers, rpc_body("ping"))
      assert json_decode!(body)["result"] == %{}
    end
  end

  defp rpc_body(method, params \\ nil, id \\ 1) do
    message = %{"jsonrpc" => "2.0", "id" => id, "method" => method}
    message = if params, do: Map.put(message, "params", params), else: message
    Jason.encode!(message)
  end

  defp post(method, params), do: Plug.Test.conn(:post, "/mcp", rpc_body(method, params))

  defp authorize(conn), do: Plug.Conn.put_req_header(conn, "authorization", "Bearer " <> @token)

  defp call(conn), do: Router.call(conn, Router.init(token: @token))

  defp rpc_call(method, params \\ nil) do
    conn = post(method, params) |> authorize() |> call()
    assert conn.status == 200
    json_decode!(conn.resp_body)
  end

  defp http_post(url, headers, body) do
    response =
      Req.post!(url,
        headers: [{"content-type", "application/json"} | headers],
        body: body,
        decode_body: false,
        retry: false
      )

    {response.status, response.body}
  end

  defp json_decode!(body), do: Jason.decode!(body)
end
