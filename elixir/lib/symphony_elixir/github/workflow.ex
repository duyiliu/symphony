defmodule SymphonyElixir.GitHub.Workflow do
  @moduledoc """
  Minimal, opt-in GitHub label workflow. One host-private JSON receipt per issue;
  no database, background worker, cache or alternative scheduler. Existing
  Symphony retries call begin_run again. Uncertain merge outcomes fail closed.
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
    if WorkflowPolicy.enabled?(settings), do: locked(settings, issue.id, &admit(issue, settings, &1, &2, opts)), else: :ok
  end

  @spec waiting?(Issue.t(), map(), keyword()) :: boolean()
  def waiting?(issue, settings, opts \\ []) do
    if WorkflowPolicy.enabled?(settings) do
      result =
        locked(settings, issue.id, fn record, _save ->
          {phase, event} = signal!(settings, issue.id, opts)
          record["blocked"] == true and record["cycle"] == "#{phase}:#{event["id"]}"
        end)

      # Failed refreshes must not release a paused task.
      result != false
    else
      false
    end
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

  defp admit(issue, settings, record, save, opts) do
    if record["pending_merge"] do
      resolve_merge(settings, issue.id, record, save, opts)
      :skip
    else
      admit_labeled(issue, settings, record, save, opts)
    end
  end

  defp admit_labeled(issue, settings, record, save, opts) do
    {phase, event} = signal!(settings, issue.id, opts)
    ensure(phase == WorkflowPolicy.phase(issue.labels), :execution_phase_changed)
    cycle = "#{phase}:#{event["id"]}"
    record = new_cycle(record, cycle, phase)

    cond do
      record["blocked"] ->
        :waiting

      record["attempts"] >= Map.get(WorkflowPolicy.config(settings), "max_attempts", 3) ->
        pause(settings, issue.id, record, save, "Worker attempt limit reached; human intervention required", opts)
        :waiting

      get_in(record, ["delivery", "cycle"]) == cycle ->
        record = save.(Map.update!(record, "attempts", &(&1 + 1)))
        handoff(settings, issue.id, record, save, opts)
        :skip

      true ->
        record = save.(Map.update!(record, "attempts", &(&1 + 1)))

        admit_phase(phase, settings, issue.id, event, record, save, opts)

        :ok
    end
  end

  defp admit_phase(:develop, _settings, _number, _event, _record, _save, _opts), do: :ok

  defp admit_phase(:merge, settings, number, event, record, save, opts) do
    fail_closed(settings, number, record, save, opts, fn ->
      authorize!(settings, number, event, record, opts)
    end)
  end

  defp new_cycle(record, cycle, phase) do
    if record["cycle"] == cycle do
      record
    else
      %{"cycle" => cycle, "attempts" => 0, "blocked" => false, "delivery" => if(phase == :merge, do: record["delivery"], else: nil)}
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
    {phase, event} = signal!(settings, number, opts)
    ensure(record["cycle"] == "#{phase}:#{event["id"]}" and not record["blocked"], :no_active_workflow_run)
    ensure(is_binary(args["tests"]) and String.trim(args["tests"]) != "" and byte_size(args["tests"]) <= 16_000, :missing_test_evidence)
    ensure(is_integer(args["pr"]) and linked_pr!(settings, number, opts) == args["pr"], :ambiguous_pr_association)
    pr = WorkflowAPI.pr(settings, args["pr"], opts)
    validate_pr!(settings, pr)
    sha = pr["head"]["sha"]
    previous = record["delivery"] || %{}
    same_handoff = previous["cycle"] == record["cycle"] and previous["sha"] == sha
    validate_repair!(phase, previous, sha, same_handoff)
    ensure(WorkflowAPI.pr(settings, args["pr"], opts)["head"]["sha"] == sha, :head_changed_during_delivery)

    delivery =
      if same_handoff,
        do: previous,
        else: %{
          "pr" => args["pr"],
          "sha" => sha,
          "base" => pr["base"]["ref"],
          "cycle" => record["cycle"],
          "tests" => args["tests"],
          "published" => false,
          "handoff_attempts" => 0,
          "previous_approvals" => []
        }

    record = save.(Map.put(record, "delivery", delivery))
    record = handoff(settings, number, record, save, opts)
    {:ok, %{"status" => "waiting", "delivery" => record["delivery"]}}
  end

  defp validate_repair!(:merge, previous, sha, same_handoff) do
    ensure(previous["sha"] != sha or same_handoff, :merge_delivery_requires_new_head)
  end

  defp validate_repair!(_phase, _previous, _sha, _same_handoff), do: :ok

  defp handoff(settings, number, record, save, opts) do
    d = record["delivery"]
    attempts = Map.get(d, "handoff_attempts", 0)

    if attempts >= Map.get(WorkflowPolicy.config(settings), "max_attempts", 3) do
      pause(settings, number, record, save, "Delivery handoff limit reached; human intervention required", opts)
      fail(:handoff_retry_limit)
    end

    record = save.(put_in(record, ["delivery", "handoff_attempts"], attempts + 1))
    record = publish_delivery(settings, number, record, save, opts)
    clear_cycle_label(settings, number, record, opts)
    record
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
          "Agent-reported test evidence:\n#{d["tests"]}\n\nAdd `merge-approved` to approve only this SHA.",
        opts
      )

      # Exclude labels added before or during publication. If this read/save
      # fails, the receipt remains unpublished and cannot authorize a merge.
      events = WorkflowAPI.events(settings, number, opts)
      delivery = Map.merge(d, %{"published" => true, "previous_approvals" => approval_ids(events)})
      save.(Map.put(record, "delivery", delivery))
    end
  end

  defp merge(settings, number, record, save, opts) do
    cond do
      record["merged"] -> {:ok, %{"status" => "merged"}}
      record["pending_merge"] -> resolve_merge(settings, number, record, save, opts)
      true -> guarded_merge(settings, number, record, save, opts)
    end
  end

  defp guarded_merge(settings, number, record, save, opts) do
    {phase, event} = signal!(settings, number, opts)
    ensure(phase == :merge and record["cycle"] == "merge:#{event["id"]}" and not record["blocked"], :not_a_merge_run)
    authorize!(settings, number, event, record, opts)

    if ready?(settings, record["delivery"], opts) do
      # Re-read authorization after CI inspection; GitHub also enforces expected
      # head SHA and branch protection at the actual mutation.
      {phase, event} = signal!(settings, number, opts)
      ensure(phase == :merge and record["cycle"] == "merge:#{event["id"]}", :authorization_revoked)
      authorize!(settings, number, event, record, opts)
      record = save.(Map.put(record, "pending_merge", true))
      WorkflowAPI.request(settings, "PUT", "#{WorkflowAPI.root(settings)}/pulls/#{record["delivery"]["pr"]}/merge", %{"sha" => record["delivery"]["sha"], "merge_method" => "squash"}, opts)
      resolve_merge(settings, number, record, save, opts)
    else
      {:ok, %{"status" => "waiting_for_checks"}}
    end
  end

  defp authorize!(settings, number, event, record, opts) do
    d = record["delivery"]
    ensure(is_map(d), :missing_delivery)
    ensure(d["published"] == true, :delivery_not_published)
    ensure(event["id"] not in d["previous_approvals"], :authorization_predates_delivery)
    actor = event["actor"] || %{}
    identity = WorkflowAPI.request(settings, "GET", "/user", nil, opts)
    ensure(identity["login"] == WorkflowPolicy.config(settings)["automation_login"], :automation_identity_mismatch)
    ensure(actor["type"] == "User" and is_integer(actor["id"]) and actor["id"] != identity["id"], :automation_cannot_authorize)
    repo = WorkflowAPI.request(settings, "GET", WorkflowAPI.root(settings), nil, opts)
    ensure(get_in(repo, ["permissions", "admin"]) == false, :automation_must_not_be_admin)
    ensure(linked_pr!(settings, number, opts) == d["pr"], :pr_association_changed)
    pr = WorkflowAPI.pr(settings, d["pr"], opts)
    validate_pr!(settings, pr)
    ensure(pr["head"]["sha"] == d["sha"] and pr["base"]["ref"] == d["base"], :delivery_changed)
  end

  defp ready?(settings, d, opts) do
    base = URI.encode(d["base"], &URI.char_unreserved?/1)
    branch = WorkflowAPI.request(settings, "GET", "#{WorkflowAPI.root(settings)}/branches/#{base}", nil, opts)
    ensure(branch["protected"] == true, :unprotected_target_branch)
    runs = WorkflowAPI.pages(settings, "#{WorkflowAPI.root(settings)}/commits/#{d["sha"]}/check-runs", "check_runs", opts)
    statuses = WorkflowAPI.pages(settings, "#{WorkflowAPI.root(settings)}/commits/#{d["sha"]}/statuses", nil, opts)
    checks_pass = Enum.all?(WorkflowPolicy.config(settings)["required_checks"], &check_passes?(&1, runs, statuses, d["sha"]))
    gate = WorkflowAPI.merge_gate(settings, d["pr"], opts)
    ensure(gate["headRefOid"] == d["sha"] and gate["baseRefName"] == d["base"], :delivery_changed)
    checks_pass and gate["mergeStateStatus"] == "CLEAN" and gate["reviewDecision"] not in ["REVIEW_REQUIRED", "CHANGES_REQUESTED"]
  end

  defp check_passes?(name, runs, statuses, sha) do
    runs =
      runs
      |> Enum.filter(&(&1["name"] == name))
      |> Enum.group_by(&get_in(&1, ["app", "id"]))
      |> Enum.map(fn {_app, checks} -> Enum.max_by(checks, & &1["id"]) end)

    statuses = statuses |> Enum.uniq_by(& &1["context"]) |> Enum.filter(&(&1["context"] == name))

    (runs != [] or statuses != []) and
      Enum.all?(runs, &(&1["head_sha"] == sha and &1["status"] == "completed" and &1["conclusion"] == "success")) and
      Enum.all?(statuses, &(&1["state"] == "success"))
  end

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
      pause(settings, number, record, save, "Merge result unconfirmed or different from delivery; inspect GitHub before reauthorizing", opts)
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
           :authorization_predates_delivery,
           :automation_cannot_authorize,
           :automation_identity_mismatch,
           :automation_must_not_be_admin,
           :pr_association_changed,
           :delivery_changed,
           :unexpected_base_branch,
           :unprotected_target_branch
         ] do
        pause(settings, number, record, save, inspect(reason), opts)
      end

      fail(reason)
  end

  defp pause(settings, number, record, save, reason, opts) do
    save.(Map.merge(record, %{"blocked" => true, "error" => reason}))
    clear_cycle_label(settings, number, record, opts)
    WorkflowAPI.comment(settings, number, "Paused: #{reason}\nDelivery: #{inspect(record["delivery"])}", opts)
  end

  defp clear_cycle_label(settings, number, record, opts) do
    {phase, event} = signal!(settings, number, opts)

    if record["cycle"] == "#{phase}:#{event["id"]}" do
      label = if phase == :develop, do: "agent", else: "merge-approved"
      WorkflowAPI.remove_label(settings, number, label, opts)
    end
  catch
    :throw, {:workflow_error, :issue_not_executable} -> :ok
  end

  defp clear_labels(settings, number, opts) do
    WorkflowAPI.remove_label(settings, number, "agent", opts)
    WorkflowAPI.remove_label(settings, number, "merge-approved", opts)
    :ok
  end

  defp signal!(settings, number, opts) do
    issue = WorkflowAPI.issue(settings, number, opts)
    labels = WorkflowAPI.labels(issue)
    phase = WorkflowPolicy.phase(labels)
    ensure(issue["state"] == "open" and phase in [:develop, :merge], :issue_not_executable)
    ensure(Enum.all?(settings.required_labels, &(&1 in labels)), :required_labels_removed)
    label = if phase == :develop, do: "agent", else: "merge-approved"
    event = WorkflowAPI.events(settings, number, opts) |> Enum.filter(&(get_in(&1, ["label", "name"]) == label and &1["event"] in ["labeled", "unlabeled"])) |> List.last()
    ensure(is_map(event) and event["event"] == "labeled" and is_integer(event["id"]), :missing_label_event)
    {phase, event}
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

  defp approval_ids(events), do: events |> Enum.filter(&(get_in(&1, ["label", "name"]) == "merge-approved")) |> Enum.map(& &1["id"])

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
