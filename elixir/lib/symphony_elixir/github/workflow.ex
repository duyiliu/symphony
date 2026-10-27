defmodule SymphonyElixir.GitHub.Workflow do
  @moduledoc """
  Minimal, opt-in GitHub label workflow. One host-private JSON receipt per issue;
  no database, background worker, cache or alternative scheduler. Existing
  Symphony retries call begin_run again. Uncertain merge outcomes fail closed.

  `agent` is the only execution label: the agent develops, delivers, and the
  host performs the deterministic merge admission (real checks, merge gate,
  risk rules, review approvals) before squash-merging and closing the issue.
  `needs-human` pauses for a human; both labels together never dispatch.
  """

  alias SymphonyElixir.GitHub.{WorkflowAPI, WorkflowPolicy}
  alias SymphonyElixir.Tracker.Issue
  require Logger
  import WorkflowAPI, only: [ensure: 2, fail: 1]

  @spec routable?(Issue.t(), map()) :: boolean()
  def routable?(issue, settings) do
    if WorkflowPolicy.enabled?(settings) and WorkflowPolicy.phase(issue.labels) == :invalid do
      Logger.warning("Conflicting GitHub execution labels issue_id=#{issue.id} issue_identifier=#{issue.identifier}; waiting for human correction")
    end

    WorkflowPolicy.routable?(issue, settings)
  end

  @spec record_failure(Issue.t(), map(), term()) :: :ok | {:error, term()}
  def record_failure(issue, settings, reason) do
    if WorkflowPolicy.enabled?(settings) do
      locked(settings, issue.id, fn record, save ->
        save.(Map.put(record, "last_error", String.slice(inspect(reason), 0, 4000)))
        :ok
      end)
    else
      :ok
    end
  end

  @spec begin_run(Issue.t(), map(), keyword()) :: :ok | :skip | :waiting | {:error, term()}
  def begin_run(issue, settings, opts \\ []) do
    if WorkflowPolicy.enabled?(settings) do
      case number(issue.id) do
        {:ok, number} ->
          locked(settings, number, &admit(issue, number, settings, &1, &2, opts))

        _ ->
          {:error, :invalid_github_workflow_issue}
      end
    else
      :ok
    end
  end

  @spec waiting?(Issue.t(), map(), keyword()) :: boolean()
  def waiting?(issue, settings, opts \\ []) do
    if WorkflowPolicy.enabled?(settings) do
      # Failed refreshes must not release a paused task.
      waiting_state(issue, settings, opts) != false
    else
      false
    end
  end

  defp waiting_state(issue, settings, opts) do
    case number(issue.id) do
      {:ok, number} ->
        locked(settings, number, fn record, _save -> blocked_cycle?(settings, number, record, opts) end)

      _ ->
        true
    end
  end

  defp blocked_cycle?(settings, number, record, opts) do
    {event, _labels} = signal!(settings, number, opts)
    record["blocked"] == true and record["cycle"] == cycle(event)
  end

  def execute(settings, args, opts \\ [])

  @spec execute(map(), map(), keyword()) :: {:ok, map()} | {:error, term()}
  def execute(settings, args, opts) when is_map(args) do
    with :ok <- WorkflowPolicy.validate(settings),
         true <- WorkflowPolicy.enabled?(settings) or {:error, :github_workflow_disabled},
         {:ok, number} <- number(args["issue"]) do
      locked(settings, number, &dispatch(settings, number, args, &1, &2, opts))
    end
  end

  def execute(_settings, _args, _opts), do: {:error, :invalid_arguments}

  defp admit(issue, number, settings, record, save, opts) do
    if record["pending_merge"] do
      resolve_merge(settings, number, record, save, opts)
      :skip
    else
      admit_labeled(issue, number, settings, record, save, opts)
    end
  end

  defp admit_labeled(issue, number, settings, record, save, opts) do
    {event, _labels} = signal!(settings, number, opts)
    ensure(WorkflowPolicy.phase(issue.labels) == :develop, :execution_phase_changed)
    record = new_cycle(record, cycle(event))

    cond do
      record["blocked"] ->
        :waiting

      record["attempts"] >= Map.get(WorkflowPolicy.config(settings), "max_attempts", 3) ->
        pause(settings, number, record, save, "Worker attempt limit reached; human intervention required", opts)
        :waiting

      true ->
        # Fresh develop cycle, or a re-dispatch after delivery (agent is kept):
        # the worker either develops/delivers or finishes the host-guarded merge.
        save.(Map.update!(record, "attempts", &(&1 + 1)))
        :ok
    end
  end

  defp cycle(event), do: "develop:#{event["id"]}"

  defp new_cycle(record, cycle) do
    if record["cycle"] == cycle do
      record
    else
      %{"cycle" => cycle, "attempts" => 0, "blocked" => false}
    end
  end

  defp dispatch(settings, number, args, record, save, opts) do
    case args["action"] do
      "status" ->
        {:ok, record}

      "deliver" ->
        deliver(settings, number, args, record, save, opts)

      "merge" ->
        fail_closed(settings, number, record, save, opts, fn ->
          merge(settings, number, record, save, opts)
        end)

      "pause" ->
        reason = String.slice(to_string(args["reason"] || "Human intervention required"), 0, 4000)
        pause(settings, number, record, save, reason, opts)
        {:ok, %{"status" => "paused"}}

      _ ->
        {:error, :invalid_github_workflow_action}
    end
  end

  defp deliver(settings, number, args, record, save, opts) do
    ensure(record["pending_merge"] != true, :merge_result_pending)
    {event, _labels} = signal!(settings, number, opts)
    ensure(record["cycle"] == cycle(event) and not record["blocked"], :no_active_workflow_run)
    ensure(is_binary(args["tests"]) and String.trim(args["tests"]) != "" and byte_size(args["tests"]) <= 16_000, :missing_test_evidence)
    ensure(is_integer(args["pr"]) and linked_pr!(settings, number, opts) == args["pr"], :ambiguous_pr_association)
    pr = WorkflowAPI.pr(settings, args["pr"], opts)
    validate_pr!(settings, pr)
    sha = pr["head"]["sha"]
    previous = record["delivery"] || %{}
    same_delivery = previous["cycle"] == record["cycle"] and previous["sha"] == sha
    ensure(WorkflowAPI.pr(settings, args["pr"], opts)["head"]["sha"] == sha, :head_changed_during_delivery)

    delivery =
      if same_delivery,
        do: previous,
        else: %{
          "pr" => args["pr"],
          "sha" => sha,
          "base" => pr["base"]["ref"],
          "cycle" => record["cycle"],
          "tests" => args["tests"],
          "published" => false
        }

    record = save.(Map.put(record, "delivery", delivery))
    record = publish_delivery(settings, number, record, save, opts)
    {:ok, %{"status" => "delivered", "delivery" => record["delivery"]}}
  end

  defp publish_delivery(settings, number, record, save, opts) do
    d = record["delivery"]

    if d["published"] == true do
      record
    else
      WorkflowAPI.comment(
        settings,
        number,
        "Delivery PR ##{d["pr"]} @ `#{d["sha"]}` → `#{d["base"]}`\n\n" <>
          "Agent-reported test evidence:\n#{d["tests"]}\n\n" <>
          "The host will verify checks, review gates and risk rules, then merge this SHA. " <>
          "High-risk or non-converging changes pause with `needs-human`.",
        opts
      )

      delivery = Map.put(d, "published", true)
      save.(Map.put(record, "delivery", delivery))
    end
  end

  defp merge(settings, number, record, save, opts) do
    cond do
      record["merged"] ->
        {:ok, %{"status" => "merged"}}

      record["pending_merge"] ->
        resolve_merge(settings, number, record, save, opts)

      true ->
        merge_delivery(settings, number, record, save, opts)
    end
  end

  defp merge_delivery(settings, number, record, save, opts) do
    {labels, pr, delivery} = delivery_state!(settings, number, record, opts)

    case auto_ready?(settings, delivery, pr, labels, opts) do
      {:ok, :ready} ->
        commit_merge(settings, number, record, save, opts)

      {:await, kind, detail} ->
        {:ok, %{"status" => "waiting_#{kind}", "pending" => detail}}

      {:fix, reason} ->
        {:ok, %{"status" => "needs_repair", "reason" => reason}}

      {:human, reason} ->
        pause(settings, number, record, save, "Auto merge declined: #{reason}", opts)
        {:ok, %{"status" => "paused"}}
    end
  end

  defp delivery_state!(settings, number, record, opts) do
    {event, labels} = signal!(settings, number, opts)
    ensure(record["cycle"] == cycle(event) and not record["blocked"], :not_a_merge_run)

    delivery = record["delivery"]
    ensure(is_map(delivery) and delivery["cycle"] == record["cycle"], :missing_delivery)
    ensure(delivery["published"] == true, :delivery_not_published)
    ensure(linked_pr!(settings, number, opts) == delivery["pr"], :pr_association_changed)
    pr = WorkflowAPI.pr(settings, delivery["pr"], opts)
    validate_pr!(settings, pr)
    ensure(pr["head"]["sha"] == delivery["sha"] and pr["base"]["ref"] == delivery["base"], :delivery_changed)
    {labels, pr, delivery}
  end

  defp commit_merge(settings, number, record, save, opts) do
    # Re-read labels after CI/risk inspection so mid-flight label edits
    # (needs-human, agent removal, closure) cannot slip through.
    {event, _labels} = signal!(settings, number, opts)
    ensure(record["cycle"] == cycle(event), :authorization_revoked)
    record = save.(Map.put(record, "pending_merge", true))

    WorkflowAPI.request(
      settings,
      "PUT",
      "#{WorkflowAPI.root(settings)}/pulls/#{record["delivery"]["pr"]}/merge",
      %{"sha" => record["delivery"]["sha"], "merge_method" => "squash"},
      opts
    )

    resolve_merge(settings, number, record, save, opts)
  end

  # Deterministic merge admission: real checks, real merge gate, project risk
  # rules and real review approvals. Anything unverifiable goes to a human.
  defp auto_ready?(settings, d, pr, issue_labels, opts) do
    runs = WorkflowAPI.pages(settings, "#{WorkflowAPI.root(settings)}/commits/#{d["sha"]}/check-runs", "check_runs", opts)
    statuses = WorkflowAPI.pages(settings, "#{WorkflowAPI.root(settings)}/commits/#{d["sha"]}/statuses", nil, opts)
    required = WorkflowPolicy.config(settings)["required_checks"] || []
    {pending, failing} = classify_checks(required, runs, statuses, d["sha"])
    gate = WorkflowAPI.merge_gate(settings, d["pr"], opts)
    ensure(gate["headRefOid"] == d["sha"] and gate["baseRefName"] == d["base"], :delivery_changed)

    cond do
      failing != [] ->
        {:fix, "required checks failing on #{d["sha"]}: #{Enum.join(failing, ", ")}"}

      pending != [] ->
        {:await, :checks, pending}

      gate["reviewDecision"] == "CHANGES_REQUESTED" ->
        {:fix, "changes requested by a reviewer; address the review comments"}

      gate["mergeStateStatus"] == "UNKNOWN" ->
        {:await, :checks, ["mergeability"]}

      true ->
        gate_ready?(settings, d, pr, gate, issue_labels, opts)
    end
  end

  defp gate_ready?(settings, d, pr, gate, issue_labels, opts) do
    case risk_required?(settings, d, issue_labels, opts) do
      {:human, reason} ->
        {:human, reason}

      {:ok, approval_required} ->
        approval_missing = approval_required and not approved?(settings, d, pr, opts)
        final_gate(gate, approval_missing)
    end
  end

  defp final_gate(gate, approval_missing) do
    cond do
      gate["mergeStateStatus"] != "CLEAN" ->
        {:fix, "merge state #{gate["mergeStateStatus"]}; resolve conflicts or update the branch"}

      gate["reviewDecision"] == "REVIEW_REQUIRED" ->
        {:await, :review, []}

      approval_missing ->
        {:await, :review, []}

      true ->
        {:ok, :ready}
    end
  end

  defp classify_checks(required, runs, statuses, sha) do
    statuses = statuses |> Enum.uniq_by(& &1["context"])

    {pending, failing} =
      Enum.reduce(required, {[], []}, fn name, {pending, failing} ->
        latest =
          runs
          |> Enum.filter(&(&1["name"] == name))
          |> Enum.group_by(&get_in(&1, ["app", "id"]))
          |> Enum.map(fn {_app, checks} -> Enum.max_by(checks, & &1["id"]) end)

        context_statuses = Enum.filter(statuses, &(&1["context"] == name))

        cond do
          latest == [] and context_statuses == [] ->
            # No report yet: the check may not have started. Waiting, not failing.
            {[name | pending], failing}

          Enum.any?(latest, &(&1["head_sha"] != sha)) ->
            {[name | pending], failing}

          Enum.any?(latest, &(&1["status"] != "completed")) ->
            {[name | pending], failing}

          Enum.all?(latest, &(&1["conclusion"] == "success")) and Enum.all?(context_statuses, &(&1["state"] == "success")) ->
            {pending, failing}

          true ->
            {pending, [name | failing]}
        end
      end)

    {Enum.reverse(pending), Enum.reverse(failing)}
  end

  defp risk_required?(settings, d, issue_labels, opts) do
    files = WorkflowAPI.pr_files(settings, d["pr"], opts)
    max_files = WorkflowPolicy.max_auto_files(settings)

    if length(files) > max_files do
      {:human, "diff touches #{length(files)} files, above the auto-merge budget of #{max_files}; human review required"}
    else
      paths = WorkflowPolicy.review_required_paths(settings)

      risky =
        Enum.any?(files, fn file ->
          is_map(file) and path_matches?(paths, file["filename"] || "")
        end)

      {:ok, risky or WorkflowPolicy.high_risk_label() in issue_labels}
    end
  end

  defp approved?(settings, d, pr, opts) do
    author = get_in(pr, ["user", "login"])

    settings
    |> WorkflowAPI.pr_reviews(d["pr"], opts)
    |> Enum.filter(&(is_map(&1) and &1["commit_id"] == d["sha"] and is_map(&1["user"])))
    |> Enum.reduce(%{}, fn review, latest ->
      Map.put(latest, review["user"]["login"], review)
    end)
    |> Enum.any?(fn {login, review} ->
      review["state"] == "APPROVED" and review["user"]["type"] == "User" and is_binary(login) and login not in [author, nil]
    end)
  end

  @doc false
  @spec path_matches?([String.t()], String.t()) :: boolean()
  def path_matches?(patterns, path) do
    path = String.downcase(path)
    Enum.any?(patterns, fn pattern -> Regex.match?(glob_regex(pattern), path) end)
  end

  defp glob_regex(glob) do
    source =
      glob
      |> String.downcase()
      |> String.graphemes()
      |> Enum.map_join(&glob_token/1)
      |> String.replace("[^/]*[^/]*", ".*")
      |> String.replace_prefix(".*/", "(?:.*/)?")

    Regex.compile!("^" <> source <> "$")
  end

  defp glob_token("*"), do: "[^/]*"
  defp glob_token("/"), do: "/"
  defp glob_token("?"), do: "[^/]"
  defp glob_token(ch), do: Regex.escape(ch)

  defp resolve_merge(settings, number, record, save, opts) do
    d = record["delivery"]
    pr = WorkflowAPI.pr(settings, d["pr"], opts)

    if pr["merged"] == true and pr["head"]["sha"] == d["sha"] and pr["base"]["ref"] == d["base"] do
      WorkflowAPI.request(settings, "PATCH", "#{WorkflowAPI.root(settings)}/issues/#{number}", %{"state" => "closed"}, opts)
      clear_labels(settings, number, opts)
      save.(Map.merge(record, %{"pending_merge" => false, "blocked" => true, "merged" => true}))
      {:ok, %{"status" => "merged"}}
    else
      record = Map.put(record, "pending_merge", false)
      pause(settings, number, record, save, "Merge result unconfirmed or different from delivery; inspect GitHub before re-running", opts)
      {:error, :merge_not_confirmed}
    end
  end

  defp fail_closed(settings, number, record, save, opts, fun) do
    fun.()
  catch
    :throw, {:workflow_error, reason} ->
      if reason in [
           :missing_delivery,
           :delivery_not_published,
           :pr_association_changed,
           :delivery_changed,
           :unexpected_base_branch
         ] do
        pause(settings, number, record, save, inspect(reason), opts)
      end

      fail(reason)
  end

  defp pause(settings, number, record, save, reason, opts) do
    save.(Map.merge(record, %{"blocked" => true, "error" => reason}))

    WorkflowAPI.comment(
      settings,
      number,
      "Paused (needs-human): #{reason}\nDelivery: #{inspect(record["delivery"])}\n\n" <>
        "恢复方式：人工处理后，移除 `needs-human` 标签并重新添加 `agent`。重贴 `agent` 不是合并授权；" <>
        "合并前宿主会重新核验当前 SHA 的检查与有效 Review 批准。",
      opts
    )

    WorkflowAPI.add_label(settings, number, WorkflowPolicy.pause_label(), opts)
    WorkflowAPI.remove_label(settings, number, "agent", opts)
  end

  defp clear_labels(settings, number, opts) do
    WorkflowAPI.remove_label(settings, number, "agent", opts)
    WorkflowAPI.remove_label(settings, number, WorkflowPolicy.pause_label(), opts)
    WorkflowAPI.remove_label(settings, number, "merge-approved", opts)
    :ok
  end

  defp signal!(settings, number, opts) do
    issue = WorkflowAPI.issue(settings, number, opts)
    labels = WorkflowAPI.labels(issue)
    ensure(issue["state"] == "open" and WorkflowPolicy.phase(labels) == :develop, :issue_not_executable)
    ensure(Enum.all?(settings.required_labels, &(&1 in labels)), :required_labels_removed)

    event =
      WorkflowAPI.events(settings, number, opts)
      |> Enum.filter(&(get_in(&1, ["label", "name"]) == "agent" and &1["event"] in ["labeled", "unlabeled"]))
      |> List.last()

    ensure(is_map(event) and event["event"] == "labeled" and is_integer(event["id"]), :missing_label_event)
    {event, labels}
  end

  defp linked_pr!(settings, number, opts) do
    prs = Enum.filter(WorkflowAPI.linked_prs(settings, number, opts), &(&1["state"] == "OPEN"))
    ensure(length(prs) == 1, :ambiguous_pr_association)
    hd(prs)["number"]
  end

  defp validate_pr!(settings, pr) do
    ensure(pr["state"] == "open" and pr["draft"] == false, :pr_not_ready)
    ensure(get_in(pr, ["base", "ref"]) == WorkflowPolicy.config(settings)["base_branch"], :unexpected_base_branch)
    repo = WorkflowPolicy.repo(settings)
    ensure(get_in(pr, ["base", "repo", "full_name"]) == repo and get_in(pr, ["head", "repo", "full_name"]) == repo, :unexpected_pr_repository)
    ensure(is_binary(get_in(pr, ["head", "sha"])) and String.match?(pr["head"]["sha"], ~r/^[a-f0-9]{40,64}$/), :invalid_head_sha)
  end

  defp locked(settings, id, fun) do
    with :ok <- WorkflowPolicy.validate(settings), {:ok, number} <- number(id) do
      dir = WorkflowPolicy.config(settings)["state_dir"]
      path = Path.join(dir, "#{number}.json")
      :global.trans({{__MODULE__, path}, self()}, fn -> transaction(settings, path, fun) end, [node()])
    end
  rescue
    error -> {:error, {:workflow_storage, Exception.message(error)}}
  catch
    :throw, {:workflow_error, reason} -> {:error, reason}
  end

  defp transaction(settings, path, fun) do
    record = read_record!(path)
    repo = WorkflowPolicy.repo(settings)
    ensure(record == %{} or record["repo"] == repo, :receipt_repository_mismatch)

    save = fn record ->
      File.mkdir_p!(Path.dirname(path))
      File.chmod!(Path.dirname(path), 0o700)
      tmp = path <> ".tmp"
      record = Map.put(record, "repo", repo)
      File.write!(tmp, Jason.encode!(record), [:binary, :sync])
      File.chmod!(tmp, 0o600)
      File.rename!(tmp, path)
      record
    end

    fun.(record, save)
  end

  defp read_record!(path) do
    case File.read(path) do
      {:ok, bytes} ->
        case Jason.decode(bytes) do
          {:ok, record} when is_map(record) -> record
          _ -> fail(:invalid_workflow_receipt)
        end

      {:error, :enoent} ->
        %{}

      {:error, reason} ->
        fail({:workflow_receipt_read, reason})
    end
  end

  defp number(id) when is_integer(id) and id > 0, do: {:ok, id}

  defp number(id) when is_binary(id) do
    case Integer.parse(id) do
      {number, ""} when number > 0 -> {:ok, number}
      _ -> {:error, :invalid_github_workflow_issue}
    end
  end

  defp number(_id), do: {:error, :invalid_github_workflow_issue}
end
