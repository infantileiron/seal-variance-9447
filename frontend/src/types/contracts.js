/** @typedef {{id:number,code:string,name:string,temperature:number,dwell:number,pressure:number,note:string,merged_into_id:number|null}} Process */
/** @typedef {{id:number,code:string,process_id:number,lot:string,force:number,flagged:boolean,note:string}} Sample */
/** @typedef {{id:number,title:string,status:'queued'|'running'|'completed'|'failed',snapshot:object,result:object|null,recommendation:string,approved:boolean}} Assessment */
/** @typedef {{id:number,source_id:number,target_id:number,source_code:string,target_code:string,canonical_code:string,status:'migrating'|'completed'|'failed',expected_total:number,moved:number,remaining:number,actor:string,error:string}} ProcessMerge */
export const contractsVersion='seal-variance-1';
