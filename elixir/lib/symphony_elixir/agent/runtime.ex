defmodule SymphonyElixir.Agent.Runtime do
  @moduledoc """
  Behaviour for the agent execution backend that drives one issue run.

  A runtime owns one long-lived session per agent run (workspace-scoped) and
  executes turns inside it. Message payloads sent to the orchestrator stay in
  the shape described by `SymphonyElixir.AgentRunner`; runtimes are responsible
  for normalising their native events into `:event` + `:timestamp` maps.
  """

  alias SymphonyElixir.Tracker.Issue

  @type session :: term()
  @type message :: %{required(:event) => atom(), required(:timestamp) => DateTime.t(), optional(atom()) => term()}

  @doc "Starts a session bound to the given workspace."
  @callback start_session(workspace :: Path.t(), opts :: keyword()) :: {:ok, session()} | {:error, term()}

  @doc "Runs one turn; `opts[:on_message]` receives normalised message maps."
  @callback run_turn(session(), prompt :: String.t(), Issue.t(), opts :: keyword()) ::
              {:ok, map()} | {:error, term()}

  @doc "Closes the session and releases its process."
  @callback stop_session(session()) :: :ok

  @doc "Static description of what the runtime can do."
  @callback capabilities() :: map()

  @optional_callbacks capabilities: 0

  @doc "Name of the runtime, used in logs and errors."
  @callback name() :: String.t()

  @optional_callbacks name: 0
end
