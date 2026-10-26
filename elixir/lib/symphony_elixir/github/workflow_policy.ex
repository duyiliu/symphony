defmodule SymphonyElixir.GitHub.WorkflowPolicy do
  @moduledoc "GitHub-only opt-in label routing and least-authority REST tool policy."

  alias SymphonyElixir.PathSafety
  alias SymphonyElixir.Tracker.Issue

  @spec config(map()) :: map()
  def config(settings), do: Map.get(settings, :github_workflow, %{}) || %{}

  @spec enabled?(map()) :: boolean()
  def enabled?(settings), do: settings.kind == "github" and config(settings)["enabled"] == true

  @spec validate(map()) :: :ok | {:error, term()}
  def validate(settings) do
    cond do
      Map.has_key?(config(settings), "enabled") and not is_boolean(config(settings)["enabled"]) ->
        {:error, :invalid_github_workflow_enabled}

      enabled?(settings) ->
        validate_enabled(config(settings), settings.required_labels)

      true ->
        :ok
    end
  end

  defp validate_enabled(cfg, required_labels) do
    rules = [
      {cfg["isolated_agent"] == true, :github_workflow_requires_isolated_agent},
      {absolute_private_path?(cfg["state_dir"]), :invalid_github_workflow_state_dir},
      {present?(cfg["base_branch"]), :missing_github_workflow_base_branch},
      {present?(cfg["automation_login"]), :missing_github_workflow_automation_login},
      {positive?(Map.get(cfg, "max_attempts", 3)), :invalid_github_workflow_max_attempts},
      {checks?(cfg["required_checks"]), :missing_github_workflow_required_checks},
      {not Enum.any?(required_labels, &(&1 in ["agent", "merge-approved"])), :execution_labels_must_not_be_required_labels}
    ]

    case Enum.find(rules, fn {valid, _reason} -> not valid end) do
      nil -> :ok
      {false, reason} -> {:error, reason}
    end
  end

  @spec validate_runtime(map()) :: :ok | {:error, term()}
  def validate_runtime(settings) do
    if enabled?(settings.tracker), do: validate_paths(settings), else: :ok
  end

  defp validate_paths(settings) do
    with {:ok, state_dir} <- PathSafety.canonicalize(config(settings.tracker)["state_dir"]),
         {:ok, workspace} <- PathSafety.canonicalize(settings.workspace.root) do
      if state_dir == workspace or String.starts_with?(state_dir, workspace <> "/") or
           String.starts_with?(workspace, state_dir <> "/") do
        {:error, :github_workflow_store_overlaps_workspaces}
      else
        :ok
      end
    end
  end

  @spec phase([String.t()]) :: :develop | :merge | :waiting | :invalid
  def phase(labels) do
    case {"agent" in labels, "merge-approved" in labels} do
      {true, false} -> :develop
      {false, true} -> :merge
      {false, false} -> :waiting
      {true, true} -> :invalid
    end
  end

  @spec same_phase?(Issue.t(), Issue.t(), map()) :: boolean()
  def same_phase?(before, after_issue, settings) do
    not enabled?(settings) or phase(before.labels) == phase(after_issue.labels)
  end

  @spec routable?(Issue.t(), map()) :: boolean()
  def routable?(issue, settings) do
    Issue.routable?(issue, settings.required_labels) and
      (not enabled?(settings) or phase(issue.labels) in [:develop, :merge])
  end

  @spec repo(map()) :: String.t()
  def repo(settings) do
    case settings.provider["repo"] do
      "$" <> name -> System.get_env(name) || ""
      value when is_binary(value) -> value
      _ -> System.get_env("GITHUB_REPO") || ""
    end
  end

  @doc "Only narrowly scoped development mutations remain available to the agent."
  @spec allow_request(map(), String.t(), String.t(), term()) :: :ok | {:error, term()}
  def allow_request(settings, method, path, body) do
    if enabled?(settings) do
      prefix = "/repos/#{repo(settings)}"
      relative = String.replace_prefix(path, prefix <> "/", "")
      in_repo = String.starts_with?(path, prefix <> "/")
      clean = not String.contains?(path, ["%", "?", "#", "\\", "..", "//"])

      allowed = clean and in_repo and allowed_endpoint?(method, relative, body)
      if allowed, do: :ok, else: {:error, :github_workflow_policy_denied}
    else
      :ok
    end
  end

  defp allowed_endpoint?("GET", path, _body) do
    String.match?(path, ~r/^(issues|pulls|commits|branches|compare|contents|git\/trees|actions\/runs|actions\/jobs)(\/|$)/)
  end

  defp allowed_endpoint?("POST", path, body) do
    cond do
      path == "pulls" ->
        keys?(body, ~w(title body head base draft maintainer_can_modify))

      String.match?(path, ~r/^(issues\/\d+\/comments|pulls\/\d+\/comments(?:\/\d+\/replies)?)$/) ->
        keys?(body, ~w(body commit_id path position line side start_line start_side in_reply_to subject_type))

      true ->
        false
    end
  end

  defp allowed_endpoint?("PATCH", path, body) do
    String.match?(path, ~r/^(?:(?:issues|pulls)\/\d+|(?:issues|pulls)\/comments\/\d+)$/) and keys?(body, ~w(title body))
  end

  defp allowed_endpoint?(_method, _path, _body), do: false
  defp keys?(body, allowed) when is_map(body), do: Enum.all?(Map.keys(body), &(&1 in allowed))
  defp keys?(_body, _allowed), do: false
  defp present?(s), do: is_binary(s) and String.trim(s) != ""
  defp positive?(n), do: is_integer(n) and n > 0
  defp checks?(items), do: is_list(items) and items != [] and Enum.all?(items, &present?/1)

  defp absolute_private_path?(path) when is_binary(path) do
    Path.type(path) == :absolute and length(Path.split(path)) >= 3
  end

  defp absolute_private_path?(_path), do: false
end
