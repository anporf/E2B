import React from 'react';
import { DataTable } from '../common/DataTable';

export const ExportTable = ({
    open,
    onClose,
    exportList,
    onExport,
    onExclude,
    title = "Export Selected Cases"
}) => {
    return (
        <DataTable 
            open={open}
            onClose={onClose}
            itemsList={exportList}
            onAction={onExport}
            onExclude={onExclude}
            title={title}
            actionButtonLabel="Export"
            canOpenItems={true}
            showIdColumn={true}
        />
    );
};
