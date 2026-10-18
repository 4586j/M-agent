import React, { useEffect, useRef } from 'react'

interface Particle {
  x: number
  y: number
  vx: number
  vy: number
  r: number
  phase: number
  colorType: 'emerald' | 'mint' | 'indigo'
}

/**
 * 全局轻量级神经网络粒子场 (Ambient Neural Particle Canvas)
 * 隐喻 Multi-Agent 节点协同与数据流转，包含环境漫游、呼吸闪烁、近邻连线与鼠标微交互；
 * 同时兼任全局 60FPS 聚光灯 (Spotlight) 坐标协调器，免除各卡片重复监听。
 */
export const ParticleBackground: React.FC = () => {
  const canvasRef = useRef<HTMLCanvasElement | null>(null)

  useEffect(() => {
    // 降级保护：若用户偏好减少动效或处于非浏览器/jsdom 测试环境，安全退出
    if (typeof window === 'undefined') return
    if (window.matchMedia?.('(prefers-reduced-motion: reduce)')?.matches) return

    const canvas = canvasRef.current
    if (!canvas) return
    const ctx = canvas.getContext('2d')
    if (!ctx) return

    let animationFrameId: number
    let isPaused = false
    let width = (canvas.width = window.innerWidth)
    let height = (canvas.height = window.innerHeight)
    const mouse = { x: -1000, y: -1000 }

    // 自适应粒子数量：根据屏幕面积计算，在 40 ~ 65 颗之间
    const count = Math.min(65, Math.max(40, Math.floor((width * height) / 24000)))
    const particles: Particle[] = Array.from({ length: count }, (_, i) => {
      // 70% 翡翠绿, 20% 浅薄荷白, 10% 电光靛蓝
      const colorType: Particle['colorType'] =
        i % 10 === 0 ? 'indigo' : i % 5 === 0 ? 'mint' : 'emerald'

      return {
        x: Math.random() * width,
        y: Math.random() * height,
        vx: (Math.random() - 0.5) * 0.32,
        vy: (Math.random() - 0.5) * 0.32,
        r: Math.random() * 1.4 + 1.1,
        phase: Math.random() * Math.PI * 2,
        colorType,
      }
    })

    const onResize = () => {
      width = canvas.width = window.innerWidth
      height = canvas.height = window.innerHeight
    }

    const onMouseMove = (e: MouseEvent) => {
      mouse.x = e.clientX
      mouse.y = e.clientY

      // 鼠标划过 .spotlight-card 时，实时设置卡片相对坐标变量，驱动聚光灯平滑微光
      const target = (e.target as HTMLElement | null)?.closest?.('.spotlight-card') as HTMLElement | null
      if (target) {
        const rect = target.getBoundingClientRect()
        target.style.setProperty('--mouse-x', `${e.clientX - rect.left}px`)
        target.style.setProperty('--mouse-y', `${e.clientY - rect.top}px`)
      }
    }

    const onMouseLeave = () => {
      mouse.x = -1000
      mouse.y = -1000
    }

    // 页面隐藏时暂停帧循环以节省性能
    const onVisibilityChange = () => {
      if (document.hidden) {
        isPaused = true
        cancelAnimationFrame(animationFrameId)
      } else {
        isPaused = false
        animationFrameId = requestAnimationFrame(render)
      }
    }

    window.addEventListener('resize', onResize)
    window.addEventListener('mousemove', onMouseMove, { passive: true })
    window.addEventListener('mouseleave', onMouseLeave)
    document.addEventListener('visibilitychange', onVisibilityChange)

    const getColor = (type: Particle['colorType'], alpha: number): string => {
      switch (type) {
        case 'indigo':
          return `rgba(99, 102, 241, ${alpha * 0.9})`
        case 'mint':
          return `rgba(209, 250, 229, ${alpha * 0.85})`
        case 'emerald':
        default:
          return `rgba(16, 185, 129, ${alpha})`
      }
    }

    const render = () => {
      if (isPaused) return
      ctx.clearRect(0, 0, width, height)

      const len = particles.length
      for (let i = 0; i < len; i++) {
        const p = particles[i]
        p.x += p.vx
        p.y += p.vy
        p.phase += 0.018

        // 边界柔和弹回
        if (p.x < 0) {
          p.x = 0
          p.vx *= -1
        } else if (p.x > width) {
          p.x = width
          p.vx *= -1
        }
        if (p.y < 0) {
          p.y = 0
          p.vy *= -1
        } else if (p.y > height) {
          p.y = height
          p.vy *= -1
        }

        // 鼠标磁吸/微排斥柔和微交互（140px 半径）
        const dxMouse = mouse.x - p.x
        const dyMouse = mouse.y - p.y
        const distMouse = Math.hypot(dxMouse, dyMouse)
        if (distMouse < 140 && distMouse > 0) {
          const factor = (1 - distMouse / 140) * 0.35
          p.x -= (dxMouse / distMouse) * factor
          p.y -= (dyMouse / distMouse) * factor

          if (distMouse < 90) {
            ctx.beginPath()
            ctx.moveTo(p.x, p.y)
            ctx.lineTo(mouse.x, mouse.y)
            ctx.strokeStyle = `rgba(16, 185, 129, ${(1 - distMouse / 90) * 0.15})`
            ctx.lineWidth = 0.5
            ctx.stroke()
          }
        }

        // 呼吸正弦透明度
        const alpha = 0.22 + Math.sin(p.phase) * 0.14
        ctx.beginPath()
        ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2)
        ctx.fillStyle = getColor(p.colorType, alpha)
        ctx.fill()

        // 智能体协同微连线（110px 距离阈值）
        for (let j = i + 1; j < len; j++) {
          const p2 = particles[j]
          const dist = Math.hypot(p.x - p2.x, p.y - p2.y)
          if (dist < 110) {
            ctx.beginPath()
            ctx.moveTo(p.x, p.y)
            ctx.lineTo(p2.x, p2.y)
            const lineAlpha = (1 - dist / 110) * 0.11
            ctx.strokeStyle = `rgba(16, 185, 129, ${lineAlpha})`
            ctx.lineWidth = 0.55
            ctx.stroke()
          }
        }
      }

      animationFrameId = requestAnimationFrame(render)
    }

    animationFrameId = requestAnimationFrame(render)

    return () => {
      cancelAnimationFrame(animationFrameId)
      window.removeEventListener('resize', onResize)
      window.removeEventListener('mousemove', onMouseMove)
      window.removeEventListener('mouseleave', onMouseLeave)
      document.removeEventListener('visibilitychange', onVisibilityChange)
    }
  }, [])

  return (
    <canvas
      ref={canvasRef}
      className="pointer-events-none fixed inset-0 z-0 opacity-75"
      aria-hidden="true"
    />
  )
}

export default ParticleBackground
