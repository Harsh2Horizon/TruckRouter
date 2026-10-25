import fs from 'node:fs/promises';
import path from 'node:path';
import { Workbook, SpreadsheetFile } from '@oai/artifact-tool';

const output = path.resolve(process.argv[2] || 'outputs/truckroute-brand-locality-v3');
const data = JSON.parse(await fs.readFile(path.join(output, '.build/data.json'), 'utf8'));
await fs.mkdir(path.join(output, '.build/previews'), { recursive: true });
const colors = { navy: '#17324D', teal: '#127C83', gray: '#526477', pale: '#EEF5F8', red: '#FCE4DE' };
const titleFont = { name: 'Arial', size: 20, bold: true, color: '#FFFFFF' };

function col(index) {
  let value = index + 1, result = '';
  while (value > 0) { value--; result = String.fromCharCode(65 + value % 26) + result; value = Math.floor(value / 26); }
  return result;
}
const numeric = (value) => value === '' || value === undefined || value === null ? null : Number(value);
const bool = (value) => value === true || value === 'True';
function typed(value, kind) {
  if (kind === 'bool') return bool(value);
  if (['score', 'number', 'similarity', 'coordinate', 'price', 'distance'].includes(kind)) return numeric(value);
  const result = value === undefined || value === null ? '' : String(value);
  return result.startsWith('=') ? "'" + result : result;
}
function heading(sheet, title, note, last='L') {
  sheet.showGridLines = false;
  sheet.getRange(`A1:${last}2`).merge();
  sheet.getRange('A1').values = [[title]];
  sheet.getRange(`A1:${last}2`).format.fill = colors.navy;
  sheet.getRange(`A1:${last}2`).format.font = titleFont;
  sheet.getRange(`A1:${last}2`).format.rowHeight = 23;
  sheet.getRange(`A1:${last}2`).format.verticalAlignment = 'center';
  sheet.getRange(`A3:${last}4`).merge();
  sheet.getRange('A3').values = [[note]];
  sheet.getRange(`A3:${last}4`).format.font = { name: 'Arial', size: 10, color: colors.gray };
  sheet.getRange(`A3:${last}4`).format.wrapText = true;
  sheet.getRange(`A3:${last}4`).format.rowHeight = 18;
  sheet.getRange(`A3:${last}4`).format.verticalAlignment = 'center';
}
function table(sheet, title, note, rows, columns, name) {
  heading(sheet, title, note);
  const end = rows.length + 6, last = col(columns.length - 1);
  sheet.getRangeByIndexes(5, 0, 1, columns.length).values = [columns.map(c => c[1])];
  for (let offset = 0; offset < rows.length; offset += 1000) {
    const block = rows.slice(offset, offset + 1000).map(row => columns.map(([key,,kind]) => typed(row[key], kind)));
    sheet.getRangeByIndexes(6 + offset, 0, block.length, columns.length).values = block;
  }
  const used = sheet.getRange(`A6:${last}${end}`);
  used.format.font = { name: 'Arial', size: 10, color: colors.navy };
  used.format.rowHeight = 21;
  const t = sheet.tables.add(`A6:${last}${Math.max(7,end)}`, true, name);
  t.style = 'TableStyleMedium2';
  t.showFilterButton = true;
  sheet.getRange(`A6:${last}6`).format.fill = colors.navy;
  sheet.getRange(`A6:${last}6`).format.font = { name: 'Arial', size: 10, bold: true, color: '#FFFFFF' };
  sheet.getRange(`A6:${last}6`).format.wrapText = true;
  sheet.getRange(`A6:${last}6`).format.rowHeight = 42;
  columns.forEach(([key,,kind,width], index) => {
    const range = sheet.getRange(`${col(index)}7:${col(index)}${Math.max(7,end)}`);
    sheet.getRange(`${col(index)}6:${col(index)}${Math.max(7,end)}`).format.columnWidth = width || 18;
    range.format.numberFormat = ({score:'0.000', number:'0', similarity:'0.0%', coordinate:'0.000000', price:'0.00000000', distance:'#,##0.0'})[kind] || 'General';
    if (kind === 'text') range.format.numberFormat = '@';
    if (key.includes('deviation_m')) range.conditionalFormats.add('cellIs', {operator:'greaterThanOrEqual',formula:1000,format:{fill:colors.red}});
  });
  sheet.freezePanes.freezeRows(6);
  sheet.freezePanes.freezeColumns(2);
  return {sheet, columns, end, letter: key => col(columns.findIndex(c => c[0] === key))};
}
function summaryBase(sheet, title) {
  heading(sheet, title, `${data.report.locations_processed.toLocaleString()} distinct stops · ${data.report.enriched_input_rows.toLocaleString()} price observations · ${data.report.algorithm_version} · completed ${data.report.finished_at.slice(0,19).replace('T',' ')} UTC`);
  sheet.getRange('A5:L31').format.font = { name: 'Arial', size: 11, color: colors.navy };
  sheet.getRange('A5:L31').format.rowHeight = 24;
  sheet.getRange('A5:L31').format.columnWidth = 14;
  sheet.getRange('A5:A31').format.columnWidth = 28;
  sheet.getRange('D5:D31').format.columnWidth = 32;
  sheet.freezePanes.freezeRows(4);
  sheet.getRange('A18:L19').merge();
  sheet.getRange('A18').values = [['Country and state must agree after normalization; city similarity must be at least 90%. Store IDs, street numbers, postal codes and provider confidence do not add matching points.']];
  sheet.getRange('A18:L19').format.wrapText = true;
  sheet.getRange('A18:L19').format.fill = colors.pale;
  sheet.getRange('A21:L22').merge();
  sheet.getRange('A21').values = [['Results are a saved run. Review candidates retain their status. Highway/exit coordinates are approximate fuel-stop locations and do not verify a fuel station. Rerun the Python pipeline to refresh matching.']];
  sheet.getRange('A21:L22').format.wrapText = true;
  const citations = [
    ['AllThePlaces · 2026-10-10-13-32-41', 'https://data.alltheplaces.xyz/runs/2026-10-10-13-32-41/'],
    ['Overture Places · 2026-10-21.0', 'https://docs.overturemaps.org/guides/places/'],
    ['OpenStreetMap · mirror 2026-10-23', 'https://github.com/gsueur/osm-geoparquet'],
    ['Exit reference · 2026-07-13', 'https://github.com/tldev/openinterstate/releases/tag/release-2026-07-13-gha-33'],
  ];
  citations.forEach(([label,url],i) => {
    sheet.getRange(`A${25+i}:C${25+i}`).merge(); sheet.getRange(`A${25+i}`).values=[[label]];
    sheet.getRange(`D${25+i}:L${25+i}`).merge(); sheet.getRange(`D${25+i}`).values=[[url]];
  });
  sheet.getRange('A25:L28').format.font = { name:'Arial',size:9,color:colors.gray };
}
function band(sheet, range) {
  sheet.getRange(range).format.fill = colors.teal;
  sheet.getRange(range).format.font = {name:'Arial',size:11,bold:true,color:'#FFFFFF'};
}
async function finish(wb, filename, previews) {
  console.log(JSON.stringify({stage:'recalculate-and-render',workbook:filename}));
  wb.recalculate();
  const inspections = [];
  for (const [sheetName,range] of previews) {
    const inspection=await wb.inspect({kind:'region',sheetId:sheetName,range,maxChars:3000,tableMaxRows:5,tableMaxCols:8});
    inspections.push({sheet:sheetName,range,ndjson:inspection.ndjson,recordCount:inspection.recordCount});
    const png = await wb.render({sheetName,range,scale:1.3,format:'png'});
    await fs.writeFile(path.join(output,'.build/previews',`${filename}-${sheetName.replaceAll(' ','-')}-${range.replaceAll(':','_')}.png`), new Uint8Array(await png.arrayBuffer()));
  }
  const errors=await wb.inspect({kind:'match',searchTerm:'#REF!|#DIV/0!|#VALUE!|#NAME\\?|#NUM!',options:{useRegex:true,maxResults:20},maxChars:3000});
  inspections.push({errorScan:errors.ndjson,recordCount:errors.recordCount});
  await fs.writeFile(path.join(output,'.build',`${filename}-inspection.json`),JSON.stringify(inspections,null,2));
  const file = await SpreadsheetFile.exportXlsx(wb);
  await file.save(path.join(output,`${filename}.xlsx`));
  console.log(JSON.stringify({saved:filename, sheets:previews.map(p=>p[0])}));
}

async function buildAnalysis() {
  const wb = Workbook.create();
  const summary = wb.worksheets.add('Summary');
  const sources = wb.worksheets.add('Source matches');
  const deviation = wb.worksheets.add('Coordinate disagreements');
  summaryBase(summary, 'Truckstop source matching');
  const sourceCols = [
    ['external_id','OPIS stop ID','text',15],['input_name','Input business name','text',32],
    ['input_city','Input city','text',21],['input_state','Input state','text',13],['input_country','Input country','text',13],
    ['provider','Provider','text',17],['source_name','Source candidate name','text',32],
    ['source_city','Source city','text',21],['source_state','Source state','text',13],['source_country','Source country','text',13],
    ['compared_input_name','Compared input name','text',26],['compared_source_name','Compared source name','text',26],
    ['name_similarity','Name similarity','similarity',16],['city_similarity','City similarity','similarity',16],
    ['state_match','State match','similarity',14],['country_match','Country match','similarity',14],
    ['locality_all_match','All locality match','bool',17],['highway_similarity','Highway similarity','similarity',17],
    ['exit_similarity','Exit similarity','similarity',15],['applicable_weight','Applicable weight','similarity',17],
    ['name_points','Name points','score',15],['locality_points','Locality points','score',15],
    ['highway_points','Highway points','score',15],['exit_points','Exit points','score',15],
    ['score','Source score','score',15],['predicted_latitude','Source latitude','coordinate',17],
    ['predicted_longitude','Source longitude','coordinate',17],['status','Source status','text',18],
    ['eligible_point','Eligible point','bool',16],['margin','Next branch margin','score',17],
    ['gates','Matching gates','text',25],['source_id','Source record ID','text',27],
    ['input_address','Input highway / address','text',42],['source_address','Source address','text',38],
    ['source_highways','Source highway clues','text',28],['source_exits','Source exit clues','text',23],
    ['reasons','Decision reasons','text',42],['diagnostic_latitude','Rejected candidate latitude','coordinate',20],
    ['diagnostic_longitude','Rejected candidate longitude','coordinate',20],
    ['coordinate_state','State at coordinate','text',21],
    ['coordinate_state_check','State polygon check','text',25],
  ];
  const src = table(sources,'Scores by source','One row per stop and source. Rejected candidate coordinates are diagnostic only; source latitude/longitude contain eligible POI candidates.',data.sources,sourceCols,'SourceMatching');
  console.log(JSON.stringify({stage:'source-table-ready',rows:data.sources.length}));
  const c = key => src.letter(key);
  // The backend owns fuzzy comparisons and eligibility; Excel exposes the four weighted contributions.
  for (const [key,sim,weight,gate] of [
    ['name_points','name_similarity',12,null],['locality_points','city_similarity',13,'locality_all_match'],
    ['highway_points','highway_similarity',14,null],['exit_points','exit_similarity',15,null],
  ]) {
    const formulas = data.sources.map((row,index) => {
      if (!row.applicable_weight) return [''];
      const r=index+7, value=gate?`IF(${c(gate)}${r},${c(sim)}${r},0)`:`${c(sim)}${r}`;
      return [`=ROUND(100*'Summary'!$E$${weight}*${value}/${c('applicable_weight')}${r},6)`];
    });
    sources.getRange(`${c(key)}7:${c(key)}${src.end}`).formulas = formulas;
  }
  sources.getRange(`${c('score')}7:${c('score')}${src.end}`).formulas = data.sources.map((row,index)=>{
    const r=index+7;
    return [`=IF(AND(${c('eligible_point')}${r},${c('locality_all_match')}${r},${c('name_similarity')}${r}>='Summary'!$F$12),ROUND(SUM(${c('name_points')}${r}:${c('exit_points')}${r}),3),0)`];
  });
  const devCols = [
    ['external_id','OPIS stop ID','text',15],['name','Input business name','text',32],['city','City','text',21],['state','State','text',12],
    ['max_source_deviation_m','Maximum deviation (m)','distance',22],['deviation_severity','Severity','text',16],['status','Combined status','text',19],
    ['coordinate_metadata_warning','Coordinate / locality conflict','text',48],
    ...['alltheplaces','overture','osm'].flatMap(p=>[
      [`${p}_score`,`${p} score`,'score',18],[`${p}_matched_name`,`${p} name`,'text',30],
      [`${p}_latitude`,`${p} latitude`,'coordinate',20],[`${p}_longitude`,`${p} longitude`,'coordinate',20],
    ]),
    ['atp_overture_distance_m','ATP–Overture (m)','distance',21],['atp_osm_distance_m','ATP–OSM (m)','distance',20],
    ['overture_osm_distance_m','Overture–OSM (m)','distance',21],['address','Input road / exit clues','text',40],
    ['deviation_notes','Possible cause (inference)','text',65],['reasons','Decision reasons','text',45],
  ];
  const dev = table(deviation,'Coordinate disagreements of at least 1 km','Sorted by maximum pairwise distance. ≥5 km is severe. Distances compare eligible source points; their source status still applies.',data.disagreements,devCols,'CoordinateDisagreements');
  summary.getRange('A5:F5').values=[['Source','Stops','With a point','Accepted','Review','Unmatched']]; band(summary,'A5:F5');
  ['alltheplaces','overture','osm'].forEach((provider,i)=>{
    const r=i+6, p=`'Source matches'!$${c('provider')}$7:$${c('provider')}$${src.end}`;
    const status=`'Source matches'!$${c('status')}$7:$${c('status')}$${src.end}`;
    const score=`'Source matches'!$${c('score')}$7:$${c('score')}$${src.end}`;
    summary.getRange(`A${r}`).values=[[provider]];
    summary.getRange(`B${r}:F${r}`).formulas=[[
      `=COUNTIFS(${p},A${r})`,`=COUNTIFS(${p},A${r},${score},">0")`,
      `=COUNTIFS(${p},A${r},${status},"accepted")`,`=COUNTIFS(${p},A${r},${status},"needs_review")`,
      `=COUNTIFS(${p},A${r},${status},"unmatched")`,
    ]];
  });
  summary.getRange('A11:B11').values=[['Coordinate comparison','Stops']]; band(summary,'A11:B11');
  summary.getRange('A12:B15').values=[
    ['At least two source points',data.report.coordinate_deviation.compared_stops],
    ['At least 1 km apart',data.disagreements.length],
    ['At least 5 km apart',data.report.coordinate_deviation.severe_at_5km],
    ['High-score disagreement',data.report.coordinate_deviation.high_score_review_stops],
  ];
  summary.getRange('A16:B16').values=[['Coordinate/locality conflicts',data.report.coordinate_metadata_conflicts]];
  summary.getRange('D11:F11').values=[['Scoring criterion','Weight','Minimum']]; band(summary,'D11:F11');
  summary.getRange('D12:F15').values=[['Name / brand',0.60,0.70],['City + state + country',0.30,0.90],['Highway',0.07,null],['Scoped exit',0.03,null]];
  summary.getRange('E12:F15').format.numberFormat='0%';
  summary.getRange('H11:L15').merge(); summary.getRange('H11').values=[['Name and locality are always required. Only highway/exit clues present in the input enter the denominator. Missing provider clues earn zero points. No store-ID points or bonuses.']]; summary.getRange('H11:L15').format.wrapText=true;
  await finish(wb,'source-matching-analysis',[
    ['Summary','A1:L28'],['Source matches','A1:L15'],['Source matches','M6:AD15'],
    ['Coordinate disagreements','A1:L15'],['Coordinate disagreements','H6:V15'],
  ]);
}

async function buildFinal() {
  const wb=Workbook.create(), summary=wb.worksheets.add('Summary'), stops=wb.worksheets.add('Final stops'), prices=wb.worksheets.add('Fuel prices');
  summaryBase(summary,'Truckstop geocoding results');
  const predictionCols=[
    ['predicted_latitude','Predicted latitude','coordinate',19],['predicted_longitude','Predicted longitude','coordinate',19],
    ['status','Status','text',19],['prediction_method','Location method','text',19],['prediction_source','Prediction source','text',25],
    ['prediction_score','Prediction score','score',18],['is_approximate','Approximate','bool',17],
    ['highest_matching_score','Highest source score','score',19],['highest_matching_source','Highest-scoring source','text',25],
    ['highest_matching_tied_sources','Tied highest sources','text',32],
    ['max_source_deviation_m','Maximum source deviation (m)','distance',24],['deviation_severity','Deviation severity','text',20],
    ['fallback_highway','Approximate highway','text',23],['fallback_exit','Approximate exit','text',19],
    ['fallback_reason','Unresolved / fallback reason','text',42],
    ['coordinate_metadata_warning','Coordinate / locality conflict','text',48],
  ];
  const stopCols=[
    ['external_id','OPIS stop ID','text',15],['name','Input business name','text',34],['city','City','text',22],['state','State','text',12],['country','Country','text',13],
    ...predictionCols,
    ...['alltheplaces','overture','osm'].flatMap(p=>[
      [`${p}_score`,`${p} score`,'score',18],[`${p}_latitude`,`${p} latitude`,'coordinate',20],[`${p}_longitude`,`${p} longitude`,'coordinate',20],
    ]),
    ['latitude','Accepted latitude','coordinate',20],['longitude','Accepted longitude','coordinate',20],
    ['address','Input highway / address','text',44],['reasons','Decision reasons','text',48],
  ];
  const st=table(stops,'Final locations by stop','One row per unique stop. Predicted coordinates can require review; accepted coordinates are populated only for accepted matches. Approximate exit points are identified explicitly.',data.stops,stopCols,'FinalStops');
  const priceCols=[
    ['OPIS Truckstop ID','OPIS stop ID','text',15],['Truckstop Name','Truckstop name','text',34],['Address','Original address','text',42],
    ['City','City','text',22],['State','State','text',12],['Rack ID','Rack ID','text',15],['Retail Price','Retail price','price',19],
    ...predictionCols,
  ];
  table(prices,'Original fuel-price observations','All original rows retained in order, including repeated stops. Retail prices retain eight decimal places; price currency, unit and timestamp were not supplied.',data.prices,priceCols,'FuelPriceResults');
  summary.getRange('A5:B5').values=[['Distinct stop status','Stops']]; band(summary,'A5:B5');
  ['accepted','needs_review','approximate','unmatched'].forEach((status,index)=>{
    const r=index+6;summary.getRange(`A${r}`).values=[[status]];
    summary.getRange(`B${r}`).formulas=[[`=COUNTIFS('Final stops'!$${st.letter('status')}$7:$${st.letter('status')}$${st.end},A${r})`]];
  });
  summary.getRange('D5:E5').values=[['Prediction method','Stops']]; band(summary,'D5:E5');
  ['fuel_poi','highway_exit','unresolved'].forEach((method,index)=>{
    const r=index+6;summary.getRange(`D${r}`).values=[[method]];
    summary.getRange(`E${r}`).formulas=[[`=COUNTIFS('Final stops'!$${st.letter('prediction_method')}$7:$${st.letter('prediction_method')}$${st.end},D${r})`]];
  });
  summary.getRange('A12:B15').values=[['Distinct stops',data.stops.length],['Original price rows',data.prices.length],['At least 1 km deviation',data.report.coordinate_deviation.flagged_at_1km],['At least 5 km deviation',data.report.coordinate_deviation.severe_at_5km]];
  summary.getRange('A16:B16').values=[['Coordinate/locality conflicts',data.report.coordinate_metadata_conflicts]];
  summary.getRange('D12:L15').merge();summary.getRange('D12').values=[['Approximate fallback currently covers verified US interstate exits. It requires the specified highway, exit, state and a city anchor within 20 km. Canadian and other unsupported exits remain unresolved when no eligible POI is found.']]; summary.getRange('D12:L15').format.wrapText=true;
  await finish(wb,'final-geocoded-locations',[
    ['Summary','A1:L28'],['Final stops','A1:L15'],['Fuel prices','A1:L15'],
  ]);
}
await buildAnalysis();
await buildFinal();
