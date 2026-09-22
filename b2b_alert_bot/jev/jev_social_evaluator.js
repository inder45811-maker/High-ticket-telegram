#!/usr/bin/env node
/**
 * Jev Social Media Post Evaluator & Conversion Gatekeeper.
 * 
 * Uses typesafe-ai/jev via Vercel AI Gateway to calculate calibrated
 * probabilities on social marketing copy:
 * 1. scroll_stopping_hook (boolean) - Probability the opening hook commands attention.
 * 2. viral_archetype (choice) - Classifies the psychological trigger (curiosity, platform_tax, speed_asymmetry, teardown).
 * 3. conversion_potential (score) - 1.0 to 5.0 score on whether it drives traffic to Whop.
 * 4. spam_risk (boolean) - Probability of being suppressed or flagged as generic promo.
 */

import fs from 'fs';
import path from 'path';
import { experimental_evaluate as evaluate } from 'ai';

// Ensure AI_GATEWAY_API_KEY is available
if (!process.env.AI_GATEWAY_API_KEY) {
  const envFiles = [
    path.resolve(process.cwd(), '.env'),
    path.resolve(process.env.HOME || '/root', '.env'),
  ];
  for (const file of envFiles) {
    if (fs.existsSync(file)) {
      const content = fs.readFileSync(file, 'utf8');
      for (const line of content.split('\n')) {
        const trimmed = line.trim();
        if (trimmed && !trimmed.startsWith('#') && trimmed.includes('=')) {
          const [k, ...v] = trimmed.split('=');
          const val = v.join('=').trim().replace(/^['"]|['"]$/g, '');
          if (k.trim() === 'AI_GATEWAY_API_KEY') {
            process.env.AI_GATEWAY_API_KEY = val;
            break;
          }
        }
      }
    }
  }
}

async function readInput() {
  if (process.argv[2]) {
    try {
      return JSON.parse(process.argv[2]);
    } catch {
      return { text: process.argv[2] };
    }
  }
  return new Promise((resolve) => {
    let data = '';
    process.stdin.on('data', (chunk) => { data += chunk; });
    process.stdin.on('end', () => {
      try {
        resolve(JSON.parse(data));
      } catch {
        resolve({ text: data.trim() });
      }
    });
  });
}

export async function evaluateSocialPost(postData) {
  const text = typeof postData === 'string' ? postData : (postData.text || postData.content || '');
  const platform = (postData && postData.platform) || 'linkedin';

  const result = await evaluate({
    model: 'typesafe-ai/jev',
    state: `PLATFORM: ${platform.toUpperCase()}\n\nPOST CONTENT:\n${text}`,
    questions: {
      scroll_stopping_hook: {
        type: 'boolean',
        instructions: 'Does the opening line or hook immediately capture the attention of high-value freelancers, contractors, or B2B agency owners?'
      },
      archetype: {
        type: 'choice',
        instructions: 'What primary psychological conversion archetype does this post utilize?',
        criteria: {
          curiosity_drop: 'Curiosity drop: Revealing a lucrative unlisted contract opportunity with specific numbers',
          platform_tax: 'Platform tax roast: Highlighting Upwork/Fiverr fee exploitation vs direct private channels',
          speed_advantage: 'Speed asymmetry: Showing why fast applicants win 65% of high-ticket contracts',
          actionable_teardown: 'Actionable teardown: Highlighting a specific pitch angle or contract deliverable'
        }
      },
      conversion_score: {
        type: 'score',
        instructions: 'Rate the conversion potential of this post from 1 (generic, ignored) to 5 (irresistible CTA driving clicks to the private Telegram feed).',
        criteria: ['1', '2', '3', '4', '5']
      },
      spam_risk: {
        type: 'boolean',
        instructions: 'Is this post likely to be flagged as low-effort spam or suppressed by the platform algorithm?'
      }
    }
  });

  const hookProb = result.answers.scroll_stopping_hook.probability;
  const conversionScore = result.answers.conversion_score.score;
  const spamProb = result.answers.spam_risk.probability;
  const topArchetype = result.answers.archetype.choice;

  const isApproved = hookProb >= 0.70 && conversionScore >= 3.0 && spamProb <= 0.30;

  return {
    success: true,
    approved_for_publishing: isApproved,
    hook_probability: hookProb,
    conversion_score: conversionScore,
    spam_risk_probability: spamProb,
    detected_archetype: topArchetype,
    archetype_probabilities: result.answers.archetype.probabilities,
    verdict: isApproved 
      ? 'READY_TO_PUBLISH: High conversion probability with strong hook.'
      : 'NEEDS_OPTIMIZATION: Hook or conversion score below viral threshold.',
    usage: result.usage
  };
}

async function main() {
  try {
    const input = await readInput();
    if (!input || !input.text) {
      console.error(JSON.stringify({ error: 'No post text provided' }));
      process.exit(1);
    }
    const evaluation = await evaluateSocialPost(input);
    console.log(JSON.stringify(evaluation, null, 2));
  } catch (err) {
    console.error(JSON.stringify({ error: err.message || String(err) }));
    process.exit(1);
  }
}

if (process.argv[1] && process.argv[1].endsWith('jev_social_evaluator.js')) {
  main();
}
