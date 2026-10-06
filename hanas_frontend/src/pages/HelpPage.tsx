import {
  Activity,
  Bell,
  Bot,
  Brain,
  Camera,
  ClipboardCheck,
  Clock3,
  Droplets,
  GitBranch,
  LifeBuoy,
  Route,
  ShieldCheck,
  UserCheck,
  Wrench,
} from 'lucide-react'
import { LiveAgentStateGraph } from '../components/ui/LiveAgentStateGraph'
import { PIPELINE_STAGE_META } from '../pipeline'
import { MobileSectionPicker } from '../components/ui/MobileSectionPicker'

const PIPELINE_HELP = [
  { key: 'orchestrator_agent', icon: Route },
  { key: 'monitoring_agent', icon: Activity },
  { key: 'diagnostic_reasoning_agent', icon: Brain },
  { key: 'decision_agent', icon: GitBranch },
  { key: 'dose_planning_agent', icon: ClipboardCheck },
  { key: 'consistency_review', icon: ClipboardCheck },
  { key: 'safety_gate', icon: ShieldCheck },
  { key: 'human_review_gate', icon: UserCheck },
] as const

const HELP_TOPICS = [
  {
    icon: Droplets,
    title: 'What HANAS monitors',
    body: 'HANAS reads pH, EC, Water Temperature, and Reservoir Volume. pH and EC drive dosing decisions. Temperature and water level provide operating context.',
  },
  {
    icon: Brain,
    title: 'What Agentic AI adds',
    body: 'Agentic AI runs as a scheduled batch. It reviews recent saved readings, explains the decision path, and prepares a recommendation that must still pass deterministic safety checks.',
  },
  {
    icon: ShieldCheck,
    title: 'Why the safety gate matters',
    body: 'The per-minute guard checks emergency thresholds, active pump commands, mixing windows, max dose, and max duration. AI can recommend, but hard rules decide what can reach the ESP32.',
  },
  {
    icon: Bot,
    title: 'Overview summarizer agent',
    body: 'The summarizer runs at 12:00 AM and 12:00 PM PHT. It turns the latest operating history into a short operator summary shown on Overview.',
  },
  {
    icon: UserCheck,
    title: 'Human-in-the-loop',
    body: 'When HITL is enabled, selected agentic dosing commands pause for operator approval, rejection, or override before physical actuation.',
  },
  {
    icon: Activity,
    title: 'Monitoring Only',
    body: 'Monitoring Only keeps live ESP32 readings stored and visible while pausing AI analysis and new pump commands. It does not interrupt a running pump. Use Emergency Stop for that. Turning Monitoring Only off requires confirmation.',
  },
  {
    icon: Wrench,
    title: 'Maintenance mode',
    body: 'Use Maintenance Mode in Settings during calibration, pump checks, water changes, or air stone checks. HANAS keeps the system visible but pauses automatic dosing.',
  },
  {
    icon: Bell,
    title: 'Alerts and logs',
    body: 'Dosing actions, skipped commands, notification results, and operator review events are stored in Logs & Alerts so operators can reconstruct what happened later.',
  },
  {
    icon: LifeBuoy,
    title: 'How to read Overview',
    body: 'Overview is the control-room view: current health, latest summary, key readings, batch safety status, and the latest Agentic AI trace. Detailed traces live in AI Reasoning.',
  },
  {
    icon: Camera,
    title: 'Live grow room camera',
    body: 'The Camera page can show the private grow-room stream from the Raspberry Pi camera gateway. Turn live view off when you do not need video to avoid loading the stream.',
  },
]

const OPERATING_MODEL = [
  {
    icon: ShieldCheck,
    title: 'Every 1 minute',
    subtitle: 'Deterministic guard',
    body: 'Each ESP32 reading is saved and checked immediately for emergency pH/EC conditions, active dosing, and mixing protection.',
  },
  {
    icon: Brain,
    title: 'Every 10 minutes',
    subtitle: 'Agentic AI batch',
    body: 'Recent readings are reviewed by the LLM pipeline. It explains monitoring, diagnosis, decision, dose planning, and final safety review.',
  },
  {
    icon: Clock3,
    title: '12:00 AM / 12:00 PM',
    subtitle: 'Summarizer agent',
    body: 'The summary agent creates a plain-language operator recap for the Overview page using the latest system history.',
  },
]

const HELP_NAV = [
  { id: 'help-guide', label: 'Guide' },
  { id: 'help-concepts', label: 'Key concepts' },
  { id: 'help-operating-model', label: 'Operating model' },
  { id: 'help-agent-pipeline', label: 'Agentic AI pipeline' },
] as const

export function HelpPage() {
  return (
    <section className="page-content help-page">
      <MobileSectionPicker label="Choose help section" sections={HELP_NAV} />
      <nav className="help-section-nav" aria-label="Help sections">
        {HELP_NAV.map((item) => (
          <a key={item.id} href={`#${item.id}`}>
            {item.label}
          </a>
        ))}
      </nav>

      <section id="help-guide" className="help-hero">
        <div>
          <span>HANAS guide</span>
          <h2>Understand the system before acting on it</h2>
          <p>
            This page explains the dashboard, the agentic AI proposal, and the safety model behind
            every dosing command.
          </p>
        </div>
      </section>

      <section id="help-concepts" className="help-topic-section" aria-label="Key help concepts">
        <div className="help-section-header compact">
          <span>Key concepts</span>
          <h2>What operators need to know first</h2>
          <p>
            Use these cards to understand what HANAS monitors, when automation pauses, and where
            to review alerts, camera feed, and operating history.
          </p>
        </div>
        <div className="help-topic-grid">
        {HELP_TOPICS.map((topic) => {
          const Icon = topic.icon
          return (
            <article key={topic.title} className="help-topic-card">
              <div className="help-topic-icon">
                <Icon size={20} strokeWidth={2.2} />
              </div>
              <h3>{topic.title}</h3>
              <p>{topic.body}</p>
            </article>
          )
        })}
        </div>
      </section>

      <section id="help-operating-model" className="help-operating-section">
        <div className="help-section-header">
          <span>Current operating model</span>
          <h2>Phase 3 separates fast safety from scheduled AI reasoning</h2>
          <p>
            The embedded system sends regular readings, the backend applies deterministic safety
            immediately, and Agentic AI reviews recent history on schedule before recommending a
            dose.
          </p>
        </div>
        <div className="help-model-grid">
          {OPERATING_MODEL.map((item) => {
            const Icon = item.icon
            return (
              <article key={item.title} className="help-model-card">
                <div className="help-topic-icon">
                  <Icon size={19} strokeWidth={2.2} />
                </div>
                <div>
                  <span>{item.title}</span>
                  <h3>{item.subtitle}</h3>
                  <p>{item.body}</p>
                </div>
              </article>
            )
          })}
        </div>
      </section>

      <section id="help-agent-pipeline" className="help-pipeline-section">
        <div className="help-section-header">
          <span>Agentic AI pipeline</span>
          <h2>How a sensor reading becomes a safe command</h2>
          <p>
            The pipeline is designed to make the decision process inspectable. Each stage has a
            specific responsibility, and the final command must still pass deterministic safety checks.
          </p>
        </div>

        <div className="help-pipeline-list">
          {PIPELINE_HELP.map(({ key, icon: Icon }, index) => {
            const stage = PIPELINE_STAGE_META[key]
            return (
              <article key={key} className="help-pipeline-item">
                <div className="help-pipeline-index">{index + 1}</div>
                <div className="help-topic-icon">
                  <Icon size={19} strokeWidth={2.2} />
                </div>
                <div>
                  <h3>{stage.label}</h3>
                  <p>{stage.operatorSummary}</p>
                </div>
              </article>
            )
          })}
        </div>

        <LiveAgentStateGraph mode="help" />
      </section>
    </section>
  )
}
