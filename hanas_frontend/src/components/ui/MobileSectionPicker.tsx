import { useEffect, useRef, useState } from 'react'
import { ChevronDown, List } from 'lucide-react'

type SectionOption = {
  id: string
  label: string
}

export function MobileSectionPicker({
  label,
  sections,
}: {
  label: string
  sections: readonly SectionOption[]
}) {
  const [activeId, setActiveId] = useState(sections[0]?.id ?? '')
  const [open, setOpen] = useState(false)
  const rootRef = useRef<HTMLDivElement>(null)
  const triggerRef = useRef<HTMLButtonElement>(null)

  const activeSection = sections.find((section) => section.id === activeId) ?? sections[0]

  const goToSection = (id: string) => {
    setActiveId(id)
    setOpen(false)
    const scrollToSection = () => {
      document.getElementById(id)?.scrollIntoView({ behavior: 'auto', block: 'start' })
    }
    scrollToSection()
    window.setTimeout(scrollToSection, 250)
  }

  useEffect(() => {
    const targets = sections
      .map((section) => document.getElementById(section.id))
      .filter((target): target is HTMLElement => Boolean(target))

    const observer = new IntersectionObserver(
      (entries) => {
        const visible = entries
          .filter((entry) => entry.isIntersecting)
          .sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top)
        if (visible[0]?.target.id) setActiveId(visible[0].target.id)
      },
      { rootMargin: '-88px 0px -62% 0px', threshold: 0 },
    )

    targets.forEach((target) => observer.observe(target))
    return () => observer.disconnect()
  }, [sections])

  useEffect(() => {
    const closeOnOutsidePress = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false)
    }
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return
      setOpen(false)
      triggerRef.current?.focus()
    }

    document.addEventListener('pointerdown', closeOnOutsidePress)
    document.addEventListener('keydown', closeOnEscape)
    return () => {
      document.removeEventListener('pointerdown', closeOnOutsidePress)
      document.removeEventListener('keydown', closeOnEscape)
    }
  }, [])

  return (
    <div className={`mobile-section-picker${open ? ' open' : ''}`} ref={rootRef}>
      <button
        ref={triggerRef}
        type="button"
        className="mobile-section-picker-trigger"
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-label={label}
        onClick={() => setOpen((current) => !current)}
      >
        <List size={18} strokeWidth={2.2} aria-hidden="true" />
        <span>{activeSection?.label}</span>
        <ChevronDown size={17} strokeWidth={2.3} aria-hidden="true" />
      </button>
      {open && (
        <div className="mobile-section-picker-menu" role="listbox" aria-label={label}>
          {sections.map((section) => (
            <button
              key={section.id}
              type="button"
              role="option"
              aria-selected={section.id === activeId}
              className={section.id === activeId ? 'selected' : ''}
              onClick={() => goToSection(section.id)}
            >
              {section.label}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
