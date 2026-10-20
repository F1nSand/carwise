<template>
  <section class="listings" aria-labelledby="listings-title">
    <div class="section-heading">
      <div>
        <h2 id="listings-title">代表性车源</h2>
      </div>
      <span>接近中位挂牌价的样本</span>
    </div>
    <div class="table-wrap">
      <table>
        <thead><tr><th scope="col">车源</th><th scope="col">城市</th><th scope="col">挂牌价</th><th scope="col">里程</th></tr></thead>
        <tbody>
          <tr v-for="listing in listings" :key="listing.carName">
            <th scope="row"><strong>{{ listing.carName }}</strong><small>{{ listing.year }} 年</small></th>
            <td>{{ listing.city }}</td>
            <td class="number">{{ formatWanAmount(listing.priceWan) }}</td>
            <td class="number">{{ listing.mileageWanKm }} 万公里</td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>
</template>

<script setup lang="ts">
import type { RepresentativeListing } from '../../types/analysis'
import { formatWanAmount } from '../../utils/formatters'

defineProps<{ listings: RepresentativeListing[] }>()
</script>

<style scoped>
.listings { padding: 22px; border: 1px solid var(--color-border); border-radius: 16px; background: var(--color-surface); }
.section-heading { display: flex; align-items: end; justify-content: space-between; gap: 16px; margin-bottom: 18px; }
h2 { margin: 0; color: var(--color-navy); font-size: 19px; letter-spacing: -0.04em; }
.section-heading > span { color: var(--color-ink-muted); font-size: 12px; }
.table-wrap { overflow-x: auto; }
table { width: 100%; min-width: 520px; border-collapse: collapse; color: var(--color-ink-soft); font-size: 12px; }
th, td { padding: 12px 10px; text-align: left; border-bottom: 1px solid var(--color-border); }
thead th { color: var(--color-ink-muted); font-size: 12px; font-weight: 650; }
tbody tr:last-child th, tbody tr:last-child td { border-bottom: 0; }
tbody th { color: var(--color-navy); font-weight: 650; }
tbody th strong, tbody th small { display: block; }
tbody th small { margin-top: 4px; color: var(--color-ink-muted); font-size: 12px; font-weight: 400; }
.number { color: var(--color-navy); font-family: var(--font-mono); font-size: 12px; white-space: nowrap; }

@media (max-width: 560px) {
  .section-heading { align-items: flex-start; flex-direction: column; gap: 5px; }
}
</style>
