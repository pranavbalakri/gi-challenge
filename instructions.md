General Intuition Research Tech Challenge
Infinite Environment Generation via an Agent Harness

Overview
- Build an agent harness that can reliably construct environments from text commands — in the form of scenes inside a game or physics engine. The agent should also be able to maneuver through the generated environments.
- We recommend starting in 2D to prove that your agent can generate and progress through environments. If your approach works, you may transfer it to 3D. Our internal vision-based policy (which you will not have access to) is needed for 3D navigation, so 2D is the more tractable starting point.

The Deliverable
- Deliver a working agent harness that is runnable in an execution context of your choice — Claude Code, Codex, or anything else. The harness must accept text-based commands and produce playable environments in a game or physics engine.
- You may solve this entirely at the prompt level, or you may need to build a custom physics engine yourself. You can be as creative as you like.

Vision-Based Policy (Context)
- For context: our vision-based policy is mounted on a game object inside the engine. It observes the environment through rendered frames and outputs controller-style actions: move forward, move backward, move left, move right, mouse delta X, and mouse delta Y. - You will not have access to this policy. This is why we suggest starting with 2D — models like Claude perform well at progressing through 2D environments on their own, whereas 3D navigation requires our policy.

Why This Matters
- This is an active research problem for us. If we can achieve infinite procedural environment generation, it unlocks several capabilities:
- Post-training environments. A massive supply of diverse environments for training and evaluating our vision-based policy on specific goals and rewards.
- Code-level objectives. Because environments are defined in code, you can encode verifiable objectives directly — such as "successfully picked up the can from the table." This is far more reliable than using a VLM on pixel output to check whether something happened.
Reward model training. Generate many environments in code space, train a reward model on the programmatic signals, and then apply that reward model to pixel-based observation — bridging the gap between code-defined truth and visual understanding.

How We Will Evaluate
- This is an open-ended problem. There is no single correct solution. We are looking for:
- Creativity. How you open the problem space and what approach you choose.
- Clarity. The submission must be self-explanatory. We will not have hours to review it should be immediately clear and digestible. 
- Working output. A harness we can actually run, with clear instructions.
- If your submission looks promising, we will schedule a call with you to go over it in more detail with our research team.

Ready to Submit?
- Send your submission to paula@generalintuition.com – subject: Tech Challenge – [Your Name]. We'll follow up within roughly a week.
- We're looking forward to seeing what you build.
