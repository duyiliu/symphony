defmodule SymphonyElixir.Agent.Runtime.Codex do
  @moduledoc """
  Codex runtime: delegates to the existing `SymphonyElixir.Codex.AppServer`
  app-server client without altering its behaviour.
  """

  alias SymphonyElixir.Codex.AppServer

  @behaviour SymphonyElixir.Agent.Runtime

  @impl true
  def start_session(workspace, opts), do: AppServer.start_session(workspace, opts)

  @impl true
  def run_turn(session, prompt, issue, opts), do: AppServer.run_turn(session, prompt, issue, opts)

  @impl true
  def stop_session(session), do: AppServer.stop_session(session)

  @impl true
  def capabilities do
    %{tools: :native, approvals: true, sandbox: true, interrupt: false, usage: :turn}
  end

  @impl true
  def name, do: "codex"
end
