defmodule SymphonyElixir.GitHub.WorkflowAPI do
  @moduledoc false

  require Logger
  alias SymphonyElixir.GitHub.{Client, WorkflowPolicy}

  @spec request(map(), String.t(), String.t(), term(), keyword()) :: term()
  def request(settings, method, path, body \\ nil, opts \\ []) do
    client = Keyword.get(opts, :github_client, &Client.request/5)
    params = Keyword.get(opts, :params, %{})

    case client.(method, path, params, body, tracker_settings: settings) do
      {:ok, %{status: status, body: result}} when status in 200..299 ->
        result

      {:ok, %{status: 404}} when method == "DELETE" ->
        %{}

      {:ok, %{status: status, body: body}} ->
        Logger.error("GitHub API error status=#{status} method=#{method} path=#{path} body=#{inspect(body, charlists: :as_lists, printable_limit: 2000)}")

        fail({:github_status, status, method, path})

      {:error, reason} ->
        fail({:github_request_failed, reason})
    end
  end

  @spec root(map()) :: String.t()
  def root(settings), do: "/repos/#{WorkflowPolicy.repo(settings)}"

  @spec issue(map(), pos_integer(), keyword()) :: map()
  def issue(settings, number, opts), do: request(settings, "GET", "#{root(settings)}/issues/#{number}", nil, opts)

  @spec pr(map(), pos_integer(), keyword()) :: map()
  def pr(settings, number, opts), do: request(settings, "GET", "#{root(settings)}/pulls/#{number}", nil, opts)

  @spec events(map(), pos_integer(), keyword()) :: [map()]
  def events(settings, number, opts), do: pages(settings, "#{root(settings)}/issues/#{number}/events", nil, opts)

  @spec pages(map(), String.t(), String.t() | nil, keyword()) :: [map()]
  def pages(settings, path, field, opts), do: pages(settings, path, field, opts, 1, [])

  defp pages(settings, path, field, opts, page, acc) when page <= 100 do
    params = Map.merge(Keyword.get(opts, :params, %{}), %{"per_page" => 100, "page" => page})
    result = request(settings, "GET", path, nil, Keyword.put(opts, :params, params))
    rows = if field, do: result[field], else: result
    ensure(is_list(rows), :invalid_github_page)

    if length(rows) < 100,
      do: Enum.concat(Enum.reverse([rows | acc])),
      else: pages(settings, path, field, opts, page + 1, [rows | acc])
  end

  defp pages(_settings, _path, _field, _opts, _page, _acc), do: fail(:github_pagination_limit)

  @doc "Use GitHub's explicit closing association, never branch-name guessing."
  @spec linked_prs(map(), pos_integer(), keyword()) :: [map()]
  def linked_prs(settings, number, opts) do
    [owner, repo] = String.split(WorkflowPolicy.repo(settings), "/", parts: 2)

    query = """
    query($owner: String!, $repo: String!, $issue: Int!) {
      repository(owner: $owner, name: $repo) {
        issue(number: $issue) {
          closedByPullRequestsReferences(first: 100, includeClosedPrs: true) {
            nodes { number state }
            pageInfo { hasNextPage }
          }
        }
      }
    }
    """

    result =
      request(
        settings,
        "POST",
        "/graphql",
        %{
          "query" => query,
          "variables" => %{"owner" => owner, "repo" => repo, "issue" => number}
        },
        opts
      )

    ensure_graphql_ok(result)
    connection = get_in(result, ["data", "repository", "issue", "closedByPullRequestsReferences"])
    ensure(is_map(connection) and get_in(connection, ["pageInfo", "hasNextPage"]) == false, :ambiguous_pr_association)
    connection["nodes"] || []
  end

  @spec merge_gate(map(), pos_integer(), keyword()) :: map()
  def merge_gate(settings, number, opts) do
    [owner, repo] = String.split(WorkflowPolicy.repo(settings), "/", parts: 2)

    query = """
    query($owner: String!, $repo: String!, $pr: Int!) {
      repository(owner: $owner, name: $repo) {
        pullRequest(number: $pr) { headRefOid baseRefName mergeStateStatus reviewDecision }
      }
    }
    """

    result =
      request(
        settings,
        "POST",
        "/graphql",
        %{
          "query" => query,
          "variables" => %{"owner" => owner, "repo" => repo, "pr" => number}
        },
        opts
      )

    ensure_graphql_ok(result)
    gate = get_in(result, ["data", "repository", "pullRequest"])
    ensure(is_map(gate), :missing_merge_gate)
    gate
  end

  @spec remove_label(map(), pos_integer(), String.t(), keyword()) :: term()
  def remove_label(settings, number, label, opts) do
    request(settings, "DELETE", "#{root(settings)}/issues/#{number}/labels/#{label}", nil, opts)
  end

  @spec comment(map(), pos_integer(), String.t(), keyword()) :: term()
  def comment(settings, number, text, opts) do
    request(settings, "POST", "#{root(settings)}/issues/#{number}/comments", %{"body" => "[agent] " <> text}, opts)
  end

  @spec labels(map()) :: [String.t()]
  def labels(issue), do: Enum.map(issue["labels"] || [], fn label -> String.downcase(label["name"]) end)

  @spec ensure(term(), term()) :: :ok
  def ensure(value, reason), do: if(value, do: :ok, else: fail(reason))

  @doc false
  @spec ensure_graphql_ok(map()) :: :ok
  def ensure_graphql_ok(result) do
    case result do
      %{"errors" => errors} ->
        Logger.error("GitHub GraphQL error detail=#{inspect(errors, charlists: :as_lists, limit: 20, printable_limit: 4000)}")

        fail(:github_graphql_error)

      _ ->
        :ok
    end
  end

  @spec fail(term()) :: no_return()
  def fail(reason), do: throw({:workflow_error, reason})
end
